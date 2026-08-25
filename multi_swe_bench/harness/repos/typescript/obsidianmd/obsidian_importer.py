import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# Headless test harness for the pre-suite era of this repository.
#
# obsidian-importer only grew an automated suite in 2026 (scripts/test.mjs
# driving `node --test` over tests/**/*.test.ts). On the commits before that,
# tests/ holds nothing but import fixtures and package.json declares no test
# script, so a run against those commits has nothing to report and every stage
# looks identical.
#
# These files close that gap without inventing a parallel implementation: they
# load the project's own modules -- through shims for `obsidian` (a types-only
# package whose runtime is the Obsidian app) and `@zip.js/zip.js` -- and assert
# the behaviour the project itself documents through its issues and its current
# expected-output fixtures. Nothing is reimplemented and no assertion is made
# about code that does not exist; a module missing at the checked-out commit is
# reported as a failing test, which is exactly the base-commit signal wanted.
#
# On any commit that does ship a test script, none of this is used: the
# repository's own suite runs instead.
# ---------------------------------------------------------------------------

_OBSIDIAN_SHIM = r"""// Stand-in for the `obsidian` module, which ships types only -- the runtime is
// the Obsidian app itself. Everything the importer touches at module scope is
// answered here so the real source can be loaded in a plain Node process.
//
// Every name is an OWN property: esbuild compiles `import { X } from 'obsidian'`
// into a copy of the module's own keys, so a Proxy get-trap alone would not be
// consulted and X would arrive undefined. The Proxy is still there to catch a
// symbol added by a later commit, but the explicit list is what makes today's
// imports resolve.

function makeMoment(input) {
	const ms = input === undefined || input === null
		? Date.now()
		: (typeof input === 'number' ? input : Date.parse(String(input)));
	const value = Number.isNaN(ms) ? 0 : ms;
	const api = {
		valueOf: () => value,
		toDate: () => new Date(value),
		unix: () => Math.floor(value / 1000),
		isValid: () => !Number.isNaN(ms),
		format: () => new Date(value).toISOString(),
		toISOString: () => new Date(value).toISOString(),
		utc: () => api,
		local: () => api,
		add: () => api,
		subtract: () => api,
	};
	return api;
}
makeMoment.utc = makeMoment;
makeMoment.unix = (s) => makeMoment(s * 1000);

// A permissive base: `class X extends Modal {}` and `new Setting(el).setName(..)`
// both keep working, since every unknown method returns the instance.
class Stub {
	constructor() {
		return new Proxy(this, {
			get: (target, prop) => {
				if (prop in target) return target[prop];
				if (typeof prop === 'symbol') return undefined;
				return () => target;
			},
		});
	}
}

const shim = {
	Platform: {
		isDesktopApp: true,
		isDesktop: true,
		isMobileApp: false,
		isMobile: false,
		isWin: process.platform === 'win32',
		isMacOS: process.platform === 'darwin',
		isLinux: process.platform === 'linux',
	},
	moment: makeMoment,
	normalizePath: (p) => String(p).replace(/\\/g, '/').replace(/\/+/g, '/'),
	htmlToMarkdown: (html) => String(html),
	sanitizeHTMLToDom: (html) => String(html),
	base64ToArrayBuffer: (b64) => {
		const buf = Buffer.from(String(b64), 'base64');
		return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
	},
	parseLinktext: (link) => {
		const [path, subpath] = String(link).split('#');
		return { path, subpath: subpath ? `#${subpath}` : '' };
	},
	getLanguage: () => 'en',
	debounce: (fn) => fn,
	setIcon: () => {},
	requestUrl: async () => ({ status: 200, text: '', json: {}, arrayBuffer: new ArrayBuffer(0) }),
	prepareFuzzySearch: () => () => null,
	renderMatches: () => {},
	sortSearchResults: () => {},
	parseYaml: () => ({}),
	stringifyYaml: (o) => JSON.stringify(o),
};

// Class-shaped exports the importer subclasses, instantiates or type-checks
// against. Listed explicitly so esbuild's own-key copy picks them up.
for (const name of [
	'AbstractInputSuggest', 'App', 'BasesConfigFile', 'BasesConfigFileView',
	'ButtonComponent', 'DropdownComponent', 'FileSystemAdapter', 'MarkdownRenderChild',
	'MarkdownRenderer', 'MarkdownView', 'Modal', 'Notice', 'Plugin', 'PluginSettingTab',
	'SearchComponent', 'SecretComponent', 'Setting', 'SettingGroup', 'SettingPage',
	'SettingTab', 'TAbstractFile', 'TFile', 'TFolder', 'ToggleComponent', 'Vault',
	'WorkspaceLeaf',
]) {
	if (!(name in shim)) shim[name] = class extends Stub {};
}

module.exports = new Proxy(shim, {
	get(target, prop) {
		if (prop in target) return target[prop];
		if (prop === '__esModule') return false;
		if (typeof prop === 'symbol') return undefined;
		return Stub;
	},
});
"""

_ZIP_SHIM = r"""// The importer only reads zip archives (Bear/Keep/Notion exports); nothing
// under test touches them, and the real package pulls a browser worker bundle
// into the graph. Names are own properties for the same reason as in the
// obsidian shim: esbuild copies own keys, so a get-trap alone is not enough --
// `class FSReader extends Reader` in filesystem.ts needs a real constructor.
class Stub {
	constructor() {
		return new Proxy(this, {
			get: (target, prop) => {
				if (prop in target) return target[prop];
				if (typeof prop === 'symbol') return undefined;
				return () => target;
			},
		});
	}
}

const shim = {
	configure: () => {},
	terminateWorkers: () => {},
};

for (const name of [
	'BlobReader', 'BlobWriter', 'Entry', 'Reader', 'TextWriter',
	'Uint8ArrayReader', 'Uint8ArrayWriter', 'Writer', 'ZipReader', 'ZipWriter',
]) {
	shim[name] = class extends Stub {};
}

module.exports = new Proxy(shim, {
	get: (target, prop) => {
		if (prop in target) return target[prop];
		if (prop === '__esModule') return false;
		if (typeof prop === 'symbol') return undefined;
		return Stub;
	},
});
"""

_HARNESS_BUILD = r"""// Bundles each module under test into a standalone CommonJS file a plain Node
// process can require, using the esbuild that ships with the commit under test.
//
// Nothing in here is allowed to throw. Every target is built on its own and a
// failure is recorded, because "this module does not exist yet" is the signal
// the base-commit stage is supposed to produce -- a crash here would instead
// leave the run with no test output at all, which the harness reads as a broken
// instance rather than as failing tests.
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import * as path from 'node:path';

const HARNESS = path.dirname(fileURLToPath(import.meta.url));
const REPO = process.env.MSB_REPO || process.cwd();
const OUT = process.env.MSB_BUILD_DIR || '/tmp/msb-build';

const TARGETS = {
	stringUtils: 'src/formats/yarle/utils/string-utils.ts',
	folderUtils: 'src/formats/yarle/utils/folder-utils.ts',
	filenameUtils: 'src/formats/yarle/utils/filename-utils.ts',
	runtimeProperties: 'src/formats/yarle/runtime-properties.ts',
	checkerFunctions: 'src/formats/yarle/utils/templates/checker-functions.ts',
	tagsYamlListPlaceholders:
		'src/formats/yarle/utils/templates/placeholders/tags-yaml-list-placeholders.ts',
	applyTagsYamlList:
		'src/formats/yarle/utils/templates/apply-functions/apply-tags-yaml-list-template.ts',
	defaultTemplate: 'src/formats/yarle/utils/templates/default-template.ts',
	filesystem: 'src/filesystem.ts',
};

// filesystem.ts reaches for Electron's node:original-fs through window.require
// at module scope; both are supplied before the bundle body runs.
const BANNER = `
globalThis.window = globalThis.window || {};
if (typeof globalThis.window.require !== 'function') {
	globalThis.window.require = (id) => require(id === 'node:original-fs' ? 'node:fs' : id);
}
globalThis.self = globalThis.self || globalThis;
`;

const status = {};
const fail = (key, reason) => {
	status[key] = { ok: false, reason: String(reason).split('\n')[0].slice(0, 300) };
};

try {
	mkdirSync(OUT, { recursive: true });
}
catch { /* the write below will report it */ }

let esbuild = null;
try {
	esbuild = createRequire(path.join(REPO, 'package.json'))('esbuild');
}
catch (err) {
	esbuild = null;
	for (const key of Object.keys(TARGETS)) {
		fail(key, `esbuild unavailable in the repository: ${err && err.message}`);
	}
}

if (esbuild) {
	for (const [key, rel] of Object.entries(TARGETS)) {
		const entry = path.join(REPO, rel);
		if (!existsSync(entry)) {
			fail(key, `not present at this commit: ${rel}`);
			continue;
		}
		try {
			await esbuild.build({
				entryPoints: [entry],
				outfile: path.join(OUT, `${key}.cjs`),
				bundle: true,
				platform: 'node',
				format: 'cjs',
				target: 'node18',
				logLevel: 'silent',
				banner: { js: BANNER },
				alias: {
					obsidian: path.join(HARNESS, 'msb-obsidian-shim.cjs'),
					'@zip.js/zip.js': path.join(HARNESS, 'msb-zip-shim.cjs'),
				},
				absWorkingDir: REPO,
			});
			status[key] = { ok: true, outfile: path.join(OUT, `${key}.cjs`) };
		}
		catch (err) {
			fail(key, (err && err.message) || err);
		}
	}
}

try {
	writeFileSync(path.join(OUT, 'status.json'), JSON.stringify(status, null, '\t'));
}
catch (err) {
	// Last resort: the spec falls back to an empty status and fails every test
	// with a clear reason, which is still a usable result.
	console.error(`could not write build status: ${err && err.message}`);
}
"""

_EVERNOTE_SPEC = r"""// Behavioural checks for the Evernote (yarle) importer, run against whatever
// the repository has checked out. They exercise the project's real modules
// through the shims in this directory -- nothing is reimplemented here.
//
// Each expectation is a statement about intended behaviour, sourced from the
// issues the project tracked (#24 notebook stacks, #26 tags in frontmatter,
// #190 dotted attachment names, #200 source URL in properties) and confirmed
// against the fixtures and expected output the project itself now ships, e.g.
// tests/evernote/expected/test-file-with-many-dots/attachments/
// test.file.with.many.dots.txt. A commit that predates the behaviour fails
// these; a commit that has it passes.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const BUILD_DIR = process.env.MSB_BUILD_DIR || '/tmp/msb-build';
// A missing status file means the build step never ran; every test then fails
// with that reason, which still reports as tests rather than as a dead run.
let status = {};
try {
	status = require(path.join(BUILD_DIR, 'status.json'));
}
catch {
	status = {};
}

// A module the checked-out commit does not have (or that does not compile) is a
// failed expectation, not a crashed run: the whole point is to tell the two
// states apart.
function load(key) {
	const entry = status[key];
	if (!entry || !entry.ok) {
		assert.fail(`module "${key}" unavailable at this commit: ${entry ? entry.reason : 'harness build did not run'}`);
	}
	return require(entry.outfile);
}

function tempDir() {
	return fs.mkdtempSync(path.join(os.tmpdir(), 'msb-yarle-'));
}

// ---------------------------------------------------------------------------
// #190 -- an attachment whose name carries several dots keeps all of them
// ---------------------------------------------------------------------------

test('an attachment name keeps every dot before its extension', () => {
	const { getResourceFileProperties } = load('filenameUtils');
	const props = getResourceFileProperties(tempDir(), {
		mime: 'text/plain',
		'resource-attributes': { 'file-name': 'test.file.with.many.dots.txt' },
	});
	assert.equal(props.fileName, 'test.file.with.many.dots.txt');
});

test('an attachment name with a single dot is unchanged', () => {
	const { getResourceFileProperties } = load('filenameUtils');
	const props = getResourceFileProperties(tempDir(), {
		mime: 'text/plain',
		'resource-attributes': { 'file-name': 'notes.txt' },
	});
	assert.equal(props.fileName, 'notes.txt');
});

// ---------------------------------------------------------------------------
// #24 -- a notebook stack is encoded in the enex name with an @@@ separator
// ---------------------------------------------------------------------------

test('a stacked notebook name splits into stack folders and a notebook', () => {
	const { getNotebookNameAndFolderNames } = load('folderUtils');
	assert.deepEqual(getNotebookNameAndFolderNames('Work@@@Projects'), {
		notebookName: 'Projects',
		notebookFolderNames: ['Work'],
	});
});

test('a nested notebook stack keeps every folder level', () => {
	const { getNotebookNameAndFolderNames } = load('folderUtils');
	assert.deepEqual(getNotebookNameAndFolderNames('Work@@@2024@@@Projects'), {
		notebookName: 'Projects',
		notebookFolderNames: ['Work', '2024'],
	});
});

test('an unstacked notebook name yields no stack folders', () => {
	const { getNotebookNameAndFolderNames } = load('folderUtils');
	assert.deepEqual(getNotebookNameAndFolderNames('Inbox'), {
		notebookName: 'Inbox',
		notebookFolderNames: [],
	});
});

test('replacing the last occurrence leaves earlier ones alone', () => {
	const { replaceLastOccurrenceInString } = load('stringUtils');
	assert.equal(replaceLastOccurrenceInString('Work/Work/Work.enex', 'Work', 'Projects'),
		'Work/Work/Projects.enex');
	assert.equal(replaceLastOccurrenceInString('Inbox.enex', 'absent', 'x'), 'Inbox.enex');
});

// ---------------------------------------------------------------------------
// #26 -- tags land in frontmatter as a YAML list, not a JSON array
// ---------------------------------------------------------------------------

test('the tags placeholder is the YAML-list one', () => {
	const P = load('tagsYamlListPlaceholders');
	assert.equal(P.CONTENT_PLACEHOLDER, '{tags-yaml-list}');
	assert.equal(P.START_BLOCK, '{tags-yaml-list-block}');
	assert.equal(P.END_BLOCK, '{end-tags-yaml-list-block}');
});

test('tags render as a YAML list with the hash stripped', () => {
	const { applyTagsYamlListTemplate } = load('applyTagsYamlList');
	const template = '{tags-yaml-list-block}tags:{tags-yaml-list}{end-tags-yaml-list-block}';
	const result = applyTagsYamlListTemplate({ tags: '#alpha #beta' }, template, () => true);
	assert.match(result, /\n {2}- alpha\n {2}- beta/);
	assert.doesNotMatch(result, /\[/, 'tags must not be serialised as a JSON array');
});

test('the template checker recognises YAML-list tags', () => {
	const checker = load('checkerFunctions');
	// hasItemInTemplate wants the opening block, the placeholder and the closing
	// block all present, so a bare placeholder is not enough to answer true.
	const template = '{tags-yaml-list-block}\ntags: {tags-yaml-list}\n{end-tags-yaml-list-block}';
	assert.equal(checker.hasAnyTagsInTemplate(template), true);
});

test('the default template asks for a YAML tag list', () => {
	const { defaultTemplate } = load('defaultTemplate');
	assert.ok(defaultTemplate.includes('{tags-yaml-list}'),
		`default template should request a YAML tag list, got: ${JSON.stringify(defaultTemplate)}`);
});

// ---------------------------------------------------------------------------
// #200 -- the web clip source URL belongs in the note's properties
// ---------------------------------------------------------------------------

test('the default template carries the source URL into frontmatter', () => {
	const { defaultTemplate } = load('defaultTemplate');
	assert.ok(defaultTemplate.includes('{source-url}'),
		`default template should carry the source URL, got: ${JSON.stringify(defaultTemplate)}`);
});

test('the default template no longer repeats the title as a heading', () => {
	const { defaultTemplate } = load('defaultTemplate');
	assert.ok(!defaultTemplate.includes('{title-block}'),
		'the note title is the filename; it should not be duplicated as an H1');
});

// ---------------------------------------------------------------------------
// Notebook identity is tracked on the runtime singleton, so a note that fails
// mid-import is reported against its notebook rather than the enex file.
// ---------------------------------------------------------------------------

test('the runtime singleton reports the current notebook name', () => {
	const { RuntimePropertiesSingleton } = load('runtimeProperties');
	const props = RuntimePropertiesSingleton.getInstance();
	props.setCurrentNotebookName('Projects');
	assert.equal(props.getCurrentNotebookName(), 'Projects');
});

test('the runtime singleton reports the current notebook fullpath', () => {
	const { RuntimePropertiesSingleton } = load('runtimeProperties');
	const props = RuntimePropertiesSingleton.getInstance();
	props.setCurrentNotebookFullpath('Work/Projects.enex');
	assert.equal(props.getCurrentNotebookFullpath(), 'Work/Projects.enex');
});

// ---------------------------------------------------------------------------
// Long-standing behaviour. These hold on both sides of the change and are here
// so a run that reports nothing but failures is distinguishable from one where
// the harness itself did not load.
// ---------------------------------------------------------------------------

test('a filepath splits into parent, name, basename and extension', () => {
	const { parseFilePath } = load('filesystem');
	assert.deepEqual(parseFilePath('path/to/my/file.md'),
		{ parent: 'path/to/my', name: 'file.md', basename: 'file', extension: 'md' });
});

test('a note title drops characters a vault filename cannot hold', () => {
	const { normalizeTitle } = load('filenameUtils');
	assert.equal(normalizeTitle('Quarterly [plan] #1 ^ref'), 'Quarterly plan 1 ref');
});

test('an empty destination directory reports no existing copies', () => {
	const { getFileIndex } = load('filenameUtils');
	assert.equal(getFileIndex(tempDir(), 'attachment'), 0);
});

test('the runtime singleton is shared', () => {
	const { RuntimePropertiesSingleton } = load('runtimeProperties');
	assert.equal(RuntimePropertiesSingleton.getInstance(), RuntimePropertiesSingleton.getInstance());
});
"""

# Package-manager detection, shared by prepare.sh (install) and the three run
# scripts (invocation) so tests always run under the manager the checked-out
# commit actually pins. obsidian-importer moved to pnpm -- package.json carries
# `"packageManager": "pnpm@10.20.0"` and the tree ships only pnpm-lock.yaml --
# so on every commit that has a test suite `npm ci` fails outright: there is no
# package-lock.json for it to read. The early commits still have one, hence the
# npm branch.
_DETECT_PM = """detect_pm() {
	if [ -f pnpm-lock.yaml ] || grep -q '"packageManager"[[:space:]]*:[[:space:]]*"pnpm@' package.json 2>/dev/null; then
		echo pnpm
	elif [ -f yarn.lock ]; then
		echo yarn
	else
		echo npm
	fi
}
"""

# Body shared verbatim by run.sh / test-run.sh / fix-run.sh, so the three can
# never drift apart: the only thing that legitimately differs between them is
# which patches get applied first.
_TEST_BODY = (
    """export CI=true
export NODE_ENV=test
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export NODE_OPTIONS="--max-old-space-size=4096"
export MSB_BUILD_DIR=/tmp/msb-build

"""
    + _DETECT_PM
    + """
PM=$(detect_pm)
if [ "$PM" != npm ]; then
	corepack enable >/dev/null 2>&1 || true
fi

# npm needs `--` before it will forward arguments to a script; pnpm and yarn
# pass them straight through.
pm_test() {
	if [ "$PM" = npm ] && [ "$#" -gt 0 ]; then
		npm test -- "$@"
	else
		"$PM" test "$@"
	fi
}

# The repository's own suite, one test FILE at a time.
#
# `node --test` emits a flat TAP stream whose points are bare test names with no
# file attached, while the names themselves repeat both across directories
# ("there are pages to convert" lives in tests/notion, tests/notion-api and
# tests/onenote) and inside a single one ("a soft return is spelled out when the
# vault has strict line breaks on" is in both tests/apple-notes/convert.test.ts
# and tests/apple-notes/settings.test.ts). An id therefore has to carry the file
# it came from, or distinct tests collapse together and a fail->pass in one can
# be hidden behind a namesake that keeps failing.
#
# Preferred path: the exact runner invocation scripts/test.mjs uses, once per
# file, announced with a marker parse_log turns into the id prefix. Where that
# is unavailable, fall back to the repo's own script per tests/<dir>, and then
# to a single unqualified run. All three stages take the same path, so ids stay
# comparable no matter which one is in play.
run_repo_suite() {
	log=/tmp/msb-repo-tests.log
	file_list=/tmp/msb-test-files.txt
	: > "$log"

	tsx_bin=node_modules/.bin/tsx
	ts_config=""
	for candidate in tsconfig.test.json tsconfig.json; do
		if [ -f "$candidate" ]; then
			ts_config="$candidate"
			break
		fi
	done

	if [ -x "$tsx_bin" ] && [ -n "$ts_config" ]; then
		find tests \\( -name '*.test.ts' -o -name '*.test.js' \\) | sort > "$file_list"
		# Read from a file rather than a pipe: a `find | while read` loop runs in
		# a subshell, and every rc=1 set inside it would be discarded on exit.
		while IFS= read -r test_file; do
			[ -n "$test_file" ] || continue
			echo "# SUITE: ${test_file#tests/}" >> "$log"
			"$tsx_bin" --disable-warning=ExperimentalWarning --tsconfig "$ts_config" --test "$test_file" >> "$log" 2>&1 || rc=1
		done < "$file_list"
	fi

	if ! grep -qE '^[[:space:]]*(ok|not ok)[[:space:]]' "$log"; then
		: > "$log"
		rc=0
		for dir in tests/*/; do
			[ -d "$dir" ] || continue
			suite=${dir%/}
			suite=${suite#tests/}
			if ! find "$dir" \\( -name '*.test.ts' -o -name '*.test.js' \\) | grep -q .; then
				continue
			fi
			echo "# SUITE: $suite" >> "$log"
			pm_test "tests/$suite" >> "$log" 2>&1 || rc=1
		done
	fi

	# Either this commit lays its tests out differently or its test script takes
	# no filter. Run the whole suite unqualified instead.
	if ! grep -qE '^[[:space:]]*(ok|not ok)[[:space:]]' "$log"; then
		: > "$log"
		rc=0
		pm_test >> "$log" 2>&1 || rc=1
	fi

	cat "$log"
	# Failures travel in rc, which run_tests returns; a non-zero status here
	# would abort the script under `set -e` before that could happen.
	return 0
}

# Commits that predate the suite: the project's own build checks, plus the
# behavioural spec in /home/msb-evernote.test.cjs run against the real modules.
run_legacy_harness() {
	echo "# SUITE: build"
	if npx --no-install tsc --noEmit --skipLibCheck; then
		echo "ok 1 - typecheck"
	else
		echo "not ok 1 - typecheck"
		rc=1
	fi
	if [ -f esbuild.config.mjs ]; then
		# The `production` argument is not optional: without it esbuild starts a
		# file watcher and the script never returns.
		if node esbuild.config.mjs production >/dev/null 2>&1; then
			echo "ok 2 - bundle"
		else
			echo "not ok 2 - bundle"
			rc=1
		fi
	else
		echo "ok 2 - bundle # SKIP no esbuild.config.mjs at this commit"
	fi

	# Only meaningful while the vendored Evernote (yarle) importer is present.
	# On a commit without it every expectation would fail identically at all
	# three stages, which is noise rather than signal.
	if [ -d src/formats/yarle ]; then
		echo "# SUITE: evernote-conversion"
		node /home/msb-build.mjs >/dev/null 2>&1 || true
		node --test /home/msb-evernote.test.cjs || rc=1
	fi
	# As in run_repo_suite: rc carries the outcome, the status must not.
	return 0
}

run_tests() {
	rc=0
	if node -e "const s=(require('./package.json').scripts)||{}; process.exit(s.test ? 0 : 1)" 2>/dev/null; then
		run_repo_suite
	else
		run_legacy_harness
	fi
	return $rc
}

run_tests
"""
)


class ObsidianImporterImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        # node:22 spans the whole repository. It has to be new enough for the
        # commits that carry the test suite -- scripts/test.mjs passes
        # `--disable-warning=ExperimentalWarning` (Node >= 20) to tsx, and pnpm
        # 10 refuses to run on Node 18 -- and it stays compatible with the early
        # commits, where npm ci on lockfileVersion 2, typescript 4.7.4 and
        # esbuild 0.17.3 all still work. The full (non-slim) tag already ships
        # git and corepack, so the clone below needs no apt layer.
        return "node:22"

    # Scoped to the PR rather than shared across the repo. A single ":base" tag
    # would be built from whichever PR happened to run first, so a later PR
    # could inherit a tree pinned to someone else's BASE_COMMIT with nothing in
    # the tag to reveal it. Tagging per PR makes the base image and the layer on
    # top of it a provable pair, and lets prepare.sh drop its re-fetch fallback.
    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ObsidianImporterImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ObsidianImporterImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(".", "msb-obsidian-shim.cjs", _OBSIDIAN_SHIM),
            File(".", "msb-zip-shim.cjs", _ZIP_SHIM),
            File(".", "msb-build.mjs", _HARNESS_BUILD),
            File(".", "msb-evernote.test.cjs", _EVERNOTE_SPEC),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# No re-fetch fallback here, deliberately. The base image is built per PR
# (tag base-pr-{pr.number}) with this PR's own BASE_COMMIT, so the commit is
# always present in the tree this layer inherits. Re-asserting the checkout
# keeps every graded run starting from a known-pristine baseline without
# re-adding the origin remote the base image's history scrub removed on
# purpose -- that scrub asserts `test -z "$(git remote)"`, and nothing below
# is allowed to undo it.
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export npm_config_audit=false
export npm_config_fund=false

{detect_pm}
PM=$(detect_pm)
echo "package manager: $PM"

# Every chain ends in `|| true`: a native module that will not compile on one
# architecture is common and usually irrelevant to the tests, and killing the
# image build over it loses the whole instance.
case "$PM" in
	pnpm)
		corepack enable || true
		# Warm the pinned pnpm into the image now: the eval container may have
		# no network, and corepack would otherwise try to fetch it at run time.
		corepack prepare --activate || true
		pnpm install --frozen-lockfile || pnpm install || true
		;;
	yarn)
		corepack enable || true
		corepack prepare --activate || true
		yarn install --frozen-lockfile || yarn install || true
		;;
	*)
		npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true
		;;
esac

# The tolerance above is deliberate, but an install that produced nothing would
# otherwise show up only as three stages of uniform failures with no stated
# cause, so name it here where it happens.
if [ ! -d node_modules ]; then
    echo "WARNING: dependency install produced no node_modules; every test stage will fail."
fi

# Fail the build now rather than at run time if the legacy harness cannot be
# compiled, but only on commits that have no suite of their own to fall back on.
if ! node -e "const s=(require('./package.json').scripts)||{{}}; process.exit(s.test ? 0 : 1)" 2>/dev/null; then
    MSB_REPO=/home/{pr.repo} MSB_BUILD_DIR=/tmp/msb-build node /home/msb-build.mjs || true
fi

node --version
npm --version

""".format(pr=self.pr, detect_pm=_DETECT_PM),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{pr.repo}
export MSB_REPO=/home/{pr.repo}
{body}""".format(pr=self.pr, body=_TEST_BODY),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{pr.repo}
export MSB_REPO=/home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
{body}""".format(pr=self.pr, body=_TEST_BODY),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{pr.repo}
export MSB_REPO=/home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true; git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
{body}""".format(pr=self.pr, body=_TEST_BODY),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("obsidianmd", "obsidian-importer")
class ObsidianImporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ObsidianImporterImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        def remove_ansi_escape_sequences(text: str) -> str:
            return re.compile(r"\x1B\[[0-?9;]*[a-zA-Z]").sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # Emitted by the run scripts before each group of tests. node's TAP gives
        # every point a bare test name with no file attached, and those names
        # repeat across importers, so the suite is what makes an id unique.
        # Absent (a whole-suite fallback run), ids stay bare -- consistently
        # across all three scripts, which is what matters.
        suite_re = re.compile(r"^#\s*SUITE:\s*(\S+)\s*$")

        # TAP 13, as emitted by node's built-in runner -- both the repository's
        # own suite and the legacy harness -- whenever stdout is not a TTY, which
        # is always the case inside the eval container:
        #
        #   ok 1 - a heading takes what is under it as its body
        #   not ok 2 - keeps block refs
        #   ok 3 - the API still returns the shape ... # SKIP set NOTION_TOKEN
        #
        # Node indents nested subtests and repeats the parent as its own point,
        # so leading whitespace is tolerated (lines are stripped first) and both
        # levels are recorded. A trailing `# SKIP` / `# TODO` directive is a real
        # outcome, not part of the name.
        tap_re = re.compile(r"^(not\s+ok|ok)\s+(?:\d+\s*)?(?:-\s*)?(.+?)\s*$")
        directive_re = re.compile(r"\s*#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        # Jest / vitest / node's `spec` reporter, kept so a future commit that
        # swaps runners still parses.
        symbol_pass_re = re.compile("^[✓✔]\\s+(.+?)$")
        symbol_fail_re = re.compile("^[✕✖✗✘×]\\s+(.+?)$")
        symbol_skip_re = re.compile("^[○●↓︎－⁃]\\s+(.+?)$")

        # Timings vary run to run; they are not part of a test's identity.
        timing_re = re.compile(r"\s*\((?:<\s*)?\d+(?:\.\d+)?\s*(?:ms|s|m)\)\s*$")

        current_suite: Optional[str] = None
        seen: dict[str, int] = {}

        def qualify(name: str) -> str:
            name = timing_re.sub("", name.strip()).strip()
            if not name:
                return ""
            base = f"{current_suite}::{name}" if current_suite else name
            # Two tests can still share a name inside one suite --
            # tests/apple-notes has "a soft return is spelled out when the vault
            # has strict line breaks on" in both convert.test.ts and
            # settings.test.ts -- and node's TAP has no file to tell them apart.
            # Number the repeats so every id is unique and the counts match
            # node's own totals. The counter advances only for identically named
            # tests, so adding unrelated tests never renumbers anything.
            occurrence = seen.get(base, 0) + 1
            seen[base] = occurrence
            return base if occurrence == 1 else f"{base} #{occurrence}"

        def record(bucket: set[str], name: str) -> None:
            test_id = qualify(name)
            if test_id:
                bucket.add(test_id)

        for raw_line in test_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("#"):
                suite_match = suite_re.match(line)
                if suite_match:
                    current_suite = suite_match.group(1)
                # `# Subtest: <name>` announcements and the trailing
                # `# tests 1625` / `# pass 1621` summary carry no outcome.
                continue

            tap_match = tap_re.match(line)
            if tap_match:
                status, name = tap_match.group(1), tap_match.group(2)
                directive = directive_re.search(name)
                name = directive_re.sub("", name)
                if directive:
                    record(skipped_tests, name)
                elif status == "ok":
                    record(passed_tests, name)
                else:
                    record(failed_tests, name)
                continue

            if line.startswith("PASS "):
                record(passed_tests, line[len("PASS ") :])
                continue
            if line.startswith("FAIL "):
                record(failed_tests, line[len("FAIL ") :])
                continue

            symbol_match = symbol_pass_re.match(line)
            if symbol_match:
                record(passed_tests, symbol_match.group(1))
                continue

            symbol_match = symbol_fail_re.match(line)
            if symbol_match:
                record(failed_tests, symbol_match.group(1))
                continue

            symbol_match = symbol_skip_re.match(line)
            if symbol_match:
                record(skipped_tests, symbol_match.group(1))
                continue

        # A name can surface under more than one status: node reports a failing
        # subtest and then the parent containing it, and a retried test appears
        # twice. Resolve precedence deterministically so the buckets never
        # overlap and the counts stay consistent with their sets.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
