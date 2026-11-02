from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: openlayers/openlayers PRs 17525..17543 (Jun-Jul 2026, main).
#
# ERA: neither unit suite has moved to Vitest yet. At both base commits of this
# interval (6eea3ed5, a9496d3c) test-browser is
#   karma start test/browser/karma.config.cjs --single-run   (webpack + mocha +
#                                                              expect.js, headless
#                                                              Chrome via puppeteer)
# and test-node is
#   mocha --recursive test/node                               (plain Mocha)
# CI pins node-version '24' (no .nvmrc yet). Later PRs run the browser suite on
# Vitest; they live in their own era files.
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-17543_to_17525"

# No .nvmrc at this commit; every CI job sets node-version: '24'.
NODE_IMAGE = "node:24-bookworm"

REPO_DIR = "/home/openlayers"

# karma.config.cjs ends with `process.env.CHROME_BIN = await
# require('puppeteer').executablePath()`, overriding any CHROME_BIN we set. That
# call honours PUPPETEER_EXECUTABLE_PATH, so the browser is chosen there. puppeteer's
# own download (Chrome for Testing) has no linux-arm64 build, so it is skipped and
# Debian's chromium -- built for both amd64 and arm64 -- is used instead.
CHROMIUM_BIN = "/usr/bin/chromium"

BROWSER_CONFIG = "test/browser/karma.config.cjs"

# Wrapper config, COPY'd to /home: loads the repo's karma.config.cjs unchanged,
# then narrows its `spec/**/*.test.js` glob to the graded files and swaps the
# `dots` reporter for a structured one. Nothing in the repo is edited.
KARMA_WRAPPER = "/home/argos_karma.cjs"

# Marker line placed directly before the normalised report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@OPENLAYERS_TEST_JSON@@"

# Recorded when a runner produced no report at all (browser failed to launch,
# webpack failed to compile a spec). Only ever FAILED: a missing report is never a
# pass.
RUN_TEST_NAME = "<run>"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    for block in re.split(r"(?m)^(?=diff --git )", patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header:
            blocks.append((header.group(2), block))
    return blocks


def _test_patch_paths(pr: PullRequest) -> list[str]:
    """Post-patch paths the test patch touches; deleted files are dropped."""
    return sorted(
        {path for path, block in _patch_blocks(pr.test_patch) if "\ndeleted file mode" not in block}
    )


def added_test_files(pr: PullRequest) -> list[str]:
    """Paths the test patch creates (`new file mode`)."""
    return sorted({path for path, block in _patch_blocks(pr.test_patch) if "\nnew file mode" in block})


def binary_stub_paths(pr: PullRequest) -> list[str]:
    """Paths whose diff is a `Binary files ... differ` stub with no payload.

    git refuses such an entry ("cannot apply binary patch ... without full index
    line") and with it the WHOLE patch, so these paths are excluded from every
    `git apply`. In this dataset they are rendering-case screenshots
    (expected.png) and raster fixtures (*.tif), none of which the graded unit
    suites read.
    """
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


def browser_test_files(pr: PullRequest) -> list[str]:
    return [
        p for p in _test_patch_paths(pr) if p.startswith("test/browser/spec/") and p.endswith(".test.js")
    ]


def node_test_files(pr: PullRequest) -> list[str]:
    return [p for p in _test_patch_paths(pr) if p.startswith("test/node/") and p.endswith(".test.js")]


# Rendering cases (test/rendering/cases/*) are deliberately NOT graded: their
# reference screenshots arrive as payload-less binary stubs, so after the excludes
# above every case compares against a missing or stale expected.png and reports a
# verdict that says nothing about the fix.


class OpenlayersImageBase(Image):
    """Shared base: toolchain + full-history clone. No pin, no scrub."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming
        # it is what would pin this shared image to one PR's commit.
        #
        # chromium comes from Debian's own archive, which builds it for amd64 and
        # arm64 alike -- never from Google's amd64-only apt repository.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CI=true \\
    NODE_OPTIONS="--max-old-space-size=4096" \\
    PUPPETEER_SKIP_DOWNLOAD=true \\
    PUPPETEER_EXECUTABLE_PATH={CHROMIUM_BIN} \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    npm_config_update_notifier=false
LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential python3 \\
        chromium fonts-liberation fonts-dejavu-core \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" {REPO_DIR}
WORKDIR {REPO_DIR}
{self.clear_env}
CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

# ========== Section 1: PIN ==========
cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
node --version

# Exactly what CI runs. Not tolerated: a failed install is a failed build.
# PUPPETEER_SKIP_DOWNLOAD (base ENV) keeps puppeteer's postinstall from fetching
# Chrome for Testing, which does not exist for linux-arm64.
npm ci

# ========== Section 3: GATE (non-tolerant, last) ==========
test -f [[BROWSER_CONFIG]]
npx karma --version
npx mocha --version
# The browser karma will launch is the one puppeteer resolves, and it runs.
# (puppeteer >= 24's executablePath() is async; karma.config.cjs awaits it too.)
node -e "(async () => { const p = await require('puppeteer').executablePath(); if (p !== '[[CHROMIUM_BIN]]') { throw new Error('puppeteer resolves ' + p); } require('fs').accessSync(p, require('fs').constants.X_OK); })().catch((err) => { console.error(err); process.exit(1); })"
# Installed and executable -- but not EXECUTED: a multi-arch build runs the other
# architecture under QEMU, whose emulated x86 CPU lacks SSE3, and chromium refuses
# to start there. Real amd64 hardware always has SSE3.
dpkg -s chromium > /dev/null
test -x [[CHROMIUM_BIN]]
# The library loads from source.
node --input-type=module -e "const m = await import('[[REPO_DIR]]/src/ol/Collection.js'); if (typeof m.default !== 'function') { throw new Error('ol did not load'); }"
echo DEPS_OK
"""


# Normalises every runner's own structured report into one line of JSON, printed
# after REPORT_MARKER. Names are `<file>::<describe> > ... > <title>`: the file plus
# the whole describe chain, never timing or counts, so the same test has the same
# name in every stage.
REPORT_CJS = r"""'use strict';
const fs = require('fs');

const ROOT = '[[REPO_DIR]]';
const [, , manifest, marker] = process.argv;

const tests = [];
const seen = new Map();
function add(name, status) {
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  tests.push({name: n > 1 ? `${name} #${n}` : name, status});
}
function rel(p) {
  p = String(p || '');
  return p.startsWith(ROOT + '/') ? p.slice(ROOT.length + 1) : p;
}
const OUTCOME = {
  passed: 'passed',
  failed: 'failed',
  skipped: 'skipped',
  pending: 'skipped',
  todo: 'skipped',
  disabled: 'skipped',
};

function karma(file, label) {
  // Karma reports no file per spec, so names are the runner label plus the
  // describe chain; the graded files are fixed per PR, so this is stable.
  const report = JSON.parse(fs.readFileSync(file, 'utf8'));
  for (const spec of report.specs || []) {
    const titles = [...(spec.suite || []), String(spec.description || '')];
    add(`${label}::${titles.join(' > ')}`, spec.status);
  }
  if (report.error) add(`${label}::[[RUN]]`, 'failed');
}

function mocha(file) {
  const report = JSON.parse(fs.readFileSync(file, 'utf8'));
  // Mocha's fullTitle is the describe chain and title joined by spaces.
  const lists = [
    ['passes', 'passed'],
    ['failures', 'failed'],
    ['pending', 'skipped'],
  ];
  for (const [key, status] of lists) {
    for (const t of report[key] || []) {
      const path = rel(t.file) || 'test/node';
      add(`${path}::${String(t.fullTitle || t.title || '')}`, status);
    }
  }
}

for (const line of fs.readFileSync(manifest, 'utf8').split('\n')) {
  if (!line.trim()) continue;
  const [kind, label, file] = line.split('\t');
  let ok = false;
  try {
    if (fs.existsSync(file) && fs.statSync(file).size > 0) {
      if (kind === 'karma') karma(file, label);
      else if (kind === 'mocha') mocha(file);
      ok = true;
    }
  } catch (err) {
    process.stderr.write(`report: ${label}: ${err.message}\n`);
  }
  if (!ok) add(`${label}::[[RUN]]`, 'failed');
}

console.log(marker);
console.log(JSON.stringify({argos_tests: tests}));
"""


KARMA_CJS = r"""'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = '[[REPO_DIR]]';
const BROWSER_DIR = path.join(ROOT, 'test', 'browser');
const upstream = require(path.join(BROWSER_DIR, 'karma.config.cjs'));

function ArgosReporter(baseReporterDecorator) {
  baseReporterDecorator(this);
  const specs = [];
  let error = false;
  this.onBrowserError = function () {
    error = true;
  };
  this.onSpecComplete = function (browser, result) {
    specs.push({
      suite: (result.suite || []).map(String),
      description: String(result.description || ''),
      status: result.skipped ? 'skipped' : result.success ? 'passed' : 'failed',
    });
  };
  this.onRunComplete = function (browsers, results) {
    if (results && results.error) {
      error = true;
    }
    fs.writeFileSync(process.env.ARGOS_KARMA_REPORT, JSON.stringify({specs, error}));
  };
}
ArgosReporter.$inject = ['baseReporterDecorator'];

module.exports = async function (karma) {
  await upstream(karma);
  const selected = (process.env.ARGOS_KARMA_FILES || '')
    .split(/\s+/)
    .filter(Boolean)
    .map((f) => ({
      pattern: path.relative(BROWSER_DIR, path.resolve(ROOT, f)),
      watched: false,
    }));
  let replaced = false;
  const files = [];
  for (const f of karma.files) {
    const pattern = typeof f === 'string' ? f : f.pattern;
    if (pattern === 'spec/**/*.test.js') {
      files.push(...selected);
      replaced = true;
    } else {
      files.push(f);
    }
  }
  if (!replaced) {
    throw new Error('karma.config.cjs no longer lists spec/**/*.test.js');
  }
  const launcher = karma.customLaunchers.ChromeHeadless;
  karma.set({
    // The upstream config sets no basePath, which karma would resolve against
    // THIS file's directory (/home); its relative patterns and proxies assume
    // test/browser.
    basePath: BROWSER_DIR,
    files,
    reporters: ['argos'],
    plugins: ['karma-*', {'reporter:argos': ['type', ArgosReporter]}],
    customLaunchers: {
      ChromeHeadless: {
        ...launcher,
        // Docker's 64 MB /dev/shm is too small for chromium; WebGL specs need
        // SwiftShader in a GPU-less container.
        flags: [...launcher.flags, '--disable-dev-shm-usage', '--enable-unsafe-swiftshader'],
      },
    },
  });
};
"""


# The one test command every stage runs. Each graded file runs only in stages where
# it exists: a test file the test patch creates is absent from the baseline, which
# then runs nothing for it.
TEST_CMD = r"""# A patch that changes the dependency manifests leaves node_modules from the base
# commit stale. Reinstall exactly when the applied patches touched them; the
# baseline and any patch that leaves them alone skip this. Not tolerated: a failed
# install fails the stage.
if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm ci
fi

_reports=/tmp/argos-reports
rm -rf "${_reports}"
mkdir -p "${_reports}"
: > "${_reports}/manifest.tsv"

_browser=()
for _f in [[BROWSER_FILES]]; do
    [ -f "${_f}" ] && _browser+=("${_f}")
done
_node=()
for _f in [[NODE_FILES]]; do
    [ -f "${_f}" ] && _node+=("${_f}")
done

# Every runner's exit status is collected rather than aborting on the first:
# all suites must report, and the stage fails if any of them failed.
_rc=0

if [ "${#_browser[@]}" -gt 0 ]; then
    printf 'karma\ttest/browser\t%s\n' "${_reports}/browser.json" >> "${_reports}/manifest.tsv"
    ARGOS_KARMA_FILES="${_browser[*]}" ARGOS_KARMA_REPORT="${_reports}/browser.json" \
        npx karma start [[KARMA_WRAPPER]] --single-run --log-level error || _rc=1
fi

if [ "${#_node[@]}" -gt 0 ]; then
    printf 'mocha\ttest/node\t%s\n' "${_reports}/node.json" >> "${_reports}/manifest.tsv"
    npx mocha --reporter json --reporter-option output="${_reports}/node.json" \
        "${_node[@]}" || _rc=1
fi

node /home/argos_report.cjs "${_reports}/manifest.tsv" "[[REPORT_MARKER]]"
exit "${_rc}"
"""


class OpenlayersImageDefault(Image):
    """Per-PR layer: patches, scripts, the pin (via prepare.sh), and the scrub."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return OpenlayersImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[BROWSER_CONFIG]]", BROWSER_CONFIG)
            .replace("[[CHROMIUM_BIN]]", CHROMIUM_BIN)
            .replace("[[KARMA_WRAPPER]]", KARMA_WRAPPER)
            .replace("[[BROWSER_FILES]]", " ".join(f"'{p}'" for p in browser_test_files(self.pr)))
            .replace("[[NODE_FILES]]", " ".join(f"'{p}'" for p in node_test_files(self.pr)))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[RUN]]", RUN_TEST_NAME)
        )

    def _apply(self, *patches: str) -> str:
        excludes = "".join(f" --exclude='{p}'" for p in binary_stub_paths(self.pr))
        return f"git apply --whitespace=nowarn{excludes} {' '.join(patches)}\n"

    def files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": self._apply("/home/test.patch"),
            "fix-run.sh": self._apply("/home/test.patch", "/home/fix.patch"),
        }
        stages = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\n" + TEST_CMD
                ),
            )
            for name, patch_step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
            File(".", "argos_report.cjs", self._expand(REPORT_CJS)),
            File(".", "argos_karma.cjs", self._expand(KARMA_CJS)),
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # PR images are never touched by DockerfileEnhancer (it returns raw the
        # moment dependency() is an Image), so everything this layer needs is
        # written out here. The scrub lives here rather than in the shared base:
        # the base must keep full history so every PR of the shard can reach its
        # own commit.
        #
        # The scrub deliberately contains no `git reset`, no `git clean` and no
        # path-scoped checkout -- prepare.sh is allowed to have modified tracked
        # files, and any of those would silently revert that work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
        return f"""FROM {name}:{tag}
{self.global_env}
{copies}RUN bash /home/prepare.sh
WORKDIR {REPO_DIR}
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{self.clear_env}
"""


@Instance.register("openlayers", "openlayers_17543_to_17525")
class OPENLAYERS_17543_TO_17525(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenlayersImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        buckets: dict[str, set[str]] = {
            "passed": set(),
            "failed": set(),
            "skipped": set(),
        }

        # The report is the first line after the marker that parses as an object
        # carrying `argos_tests`. Test output can itself print `{`-lines, so a line
        # is only accepted once it parses. A truncated or missing report yields no
        # tests for this stage rather than names guessed from console text.
        report = None
        lines = log.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != REPORT_MARKER:
                continue
            for candidate in lines[index + 1 :]:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "argos_tests" in parsed:
                    report = parsed
                    break
            break

        for entry in (report or {}).get("argos_tests") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            status = str(entry.get("status") or "")
            if name and status in buckets:
                buckets[status].add(name)

        passed = buckets["passed"]
        failed = buckets["failed"]
        skipped = buckets["skipped"]

        # A name may live in only one bucket; failure wins over both others.
        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
