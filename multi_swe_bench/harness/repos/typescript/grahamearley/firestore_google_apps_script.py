"""grahamearley/FirestoreGoogleAppsScript harness config — PR #87 only.

Toolchain: Node.js 14 / npm 6 (the June-2020 era of the PR), TypeScript 3.9,
ESLint 7 with @typescript-eslint 3 and eslint-plugin-prettier, installed from the
PR's own `package-lock.json` (lockfileVersion 1) so every version is pinned.

Image layout — the `mvdan/sh.py` / `Borewit/music_metadata.py` shape
-------------------------------------------------------------------
  base-pr-87   clone, check out this PR's base commit, then the COMPLETE history
               scrub: `Image._HARDENING_BLOCK` verbatim — gc, repack and all four
               integrity asserts. Nothing is left over for the PR layer.

  pr-87        deliberately thin: stage the patches and run scripts, provision
               the dependency tree, CMD. NO scrub block at all.

The dataset is a single PR, so this is row 1 of the base-sharing table: a per-PR
base with the full scrub inside it. The scrub opens with
`git checkout --detach "${BASE_COMMIT}"`, so it can only run where BASE_COMMIT is
a real value — and it is real here because `dependency()` returns a `str`, which
is what makes build_dataset.py:625-629 pass REPO_URL and BASE_COMMIT as build
args. An Image-dependency layer receives no build args at all. That is also why
the tag is `base-pr-87` rather than a shared era tag: `gc --prune=now` needs one
pinned HEAD, and there is exactly one PR to pin it to.

Neither patch contains a binary section (verified: zero `Binary files … differ`
markers and zero `GIT binary patch` blocks in both), so there is no blob lift and
the base needs no staged files at all.

There is no test suite. This is the whole problem.
-------------------------------------------------
`Tests.ts` is a GSUnit suite driven by the Google Apps Script runtime against a
LIVE Firestore instance: it calls `PropertiesService`, `UrlFetchApp` and
`Utilities`, and authenticates with a service-account key. The README's "Tests"
badge points at a `script.google.com/macros/s/…/exec` endpoint — the tests ran in
Google's cloud, never in CI. `package.json` has no `test` script at any commit.

Nothing in that suite can execute inside a container, and inventing a stub
runtime for it would mean authoring the tests rather than running them.

What CAN run is the repo's own `npm run lint`, i.e. `eslint --ext .ts . && tsc`.
So each `.ts` file in the worktree contributes two test ids:

    tsc::<path>       the file type-checks under some tsconfig in the repo
    eslint::<path>    the file lints clean under the repo's own eslintConfig

Measured in a container at the merge commit (38b94d6): both tsconfigs exit 0 and
all 17 files lint clean, so the fix stage is fully green.

Why the id puts the TOOL first and the path second
--------------------------------------------------
report.py's cheating guard splits a test name on `::` and treats the HEAD as a
file path (`_test_name_matches_files`). With `<path>::tsc`, every id whose file
the fix patch creates — Auth.ts, Query.ts, Firestore.ts, … — would match
`fix_patch_files`, `guard_fix_patch_touched_tests` would fill up and check 5
would reject the instance as a self-satisfying loop.

With `tsc::<path>` the head is the literal string `tsc`, which matches no file,
so `_fix_patch_matcher_ok` is False and the guard stays out of the way. It is
correct that it does: the fix patch authored no test here. It is also why
`fix_patch_authored_candidates` comes out empty — `_touched_by_test_patch` then
fails open and the 28 newly-added files land in n2p, which is what they are.

Why ESLint is run with an endOfLine override
--------------------------------------------
`package.json` sets prettier's `endOfLine: "crlf"`. The repo's git objects store
MIXED line endings — 10 of the 17 `.ts` files are CRLF, 7 are LF — and the author
worked on Windows with `core.autocrlf=true`, which made their worktree uniformly
CRLF. On Linux a plain clone reproduces the mixture, and the 7 LF files fail with
one `prettier/prettier: Insert ␍` per line.

The reconstructed worktree is worse: this dataset's patches were captured
LF-normalised (zero CRLF bytes in either patch), so after `git apply` ALL 17
files are LF and every one of them fails — 4,342 errors, one per line, in the fix
stage as well as the test stage.

That is a property of the patch transport, not of the repo and not of the change
under evaluation. Verified by rerunning the identical lint with
`endOfLine: "auto"`: every file drops to 0 errors. So the runner passes exactly
one narrowly-scoped CLI override:

    --rule {"prettier/prettier":["error",{"endOfLine":"auto"}]}

Every other prettier option and every other ESLint rule is left exactly as the
repo configured it. Without this the ESLint half of the suite would be red in all
three stages and contribute nothing but noise.

Why the dependency tree lives OUTSIDE the repo
----------------------------------------------
The base commit's `package.json` has no `dependencies` and no `scripts` at all —
eslint and typescript arrive with the fix patch. So the tree is installed once at
build time into `/home/npmdeps`, from the post-fix `package.json` /
`package-lock.json` lifted out of the fix patch, and each run script symlinks it
in as `node_modules`.

It has to live outside the repo because at the base commit `node_modules` is not
in `.gitignore` (the fix patch adds it), so a real `node_modules` directory in
the worktree would make `git status --porcelain` dirty in the shipped image. The
symlink is created at test time by the run scripts, never at build time, so the
delivered container's worktree is clean.

`npm ci` is run with `--unsafe-perm`. npm 6 refuses to run lifecycle scripts as
root and only WARNS ("cannot run in wd …"), so without the flag the repo's
`postinstall` — which comments out a duplicate `declare var console` in
`@types/google-apps-script` — is silently skipped and `tsc` then fails with
TS2403. prepare.sh asserts the replacement actually happened, so that turns into
a loud build failure rather than a mysterious type error at test time.

`git clean -fdxq` (with `-x`) is likewise not optional in prepare.sh: at the base
commit `appsscript.json` is gitignored, the fix patch adds it, and a plain
`git clean -fd` leaves it on disk. `git status` still reports clean — ignored
files are not listed — and the next `git apply` dies with "already exists in
working directory".

Why the base re-materialises the worktree after checkout
--------------------------------------------------------
This repo's CURRENT master carries a `.gitattributes` with `text eol=crlf`, so
`git clone` — which checks out the default branch first — writes
`.github/CODE_OF_CONDUCT.md` as CRLF (3270 bytes). The base commit has no
`.gitattributes` at all, but that file's blob sha is IDENTICAL at both commits,
so `git checkout ${BASE_COMMIT}` sees nothing to update and leaves the converted
bytes on disk. The worktree file then differs from its own blob (3224 bytes, LF)
while the index's cached size and mtime still match it — so `git status` reports
clean right up until something forces a content comparison.

Reproduced with nothing but `git clone` + `git checkout <base>` in a stock node
image, so it is upstream behaviour rather than anything the harness does.

It is handled in two places. The base runs `git rm --cached -r . && git reset
--hard` after the checkout — git's own recipe for an attribute change, and the
only one that rebuilds the index stat cache as well as the bytes — then asserts
both a clean `git status` and zero `w/crlf` entries. And check_git_changes.sh
runs `git update-index --really-refresh` first, so it can never again pass on
stat-cache luck: the original build passed precisely because the clone had left
the index's cached size and mtime matching the CRLF file on disk, and git took
the fast path without ever comparing content.

No `.ts` file is affected, so no test id ever depended on this — but the
delivered image would otherwise ship with a dirty worktree.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The era of the PR: June 2020, TypeScript 3.9 / ESLint 7. npm 6 is what produced
# this repo's lockfileVersion 1 `package-lock.json`, so `npm ci` takes its native
# path rather than npm 7+'s in-memory conversion.
NODE_IMAGE = "node:14-bullseye"

# Where the dependency tree is installed at build time. Outside the repo on
# purpose — see the module docstring.
NPM_DEPS_DIR = "/home/npmdeps"

# One place for the check command so run.sh / test-run.sh / fix-run.sh cannot
# drift apart.
CHECK_CMD = "node /home/run-checks.js /home/__REPO__"


# --------------------------------------------------------------------------- #
# The TAP runner.                                                             #
#                                                                             #
# Written in JavaScript rather than Python so it needs nothing the node base    #
# image does not already ship. Kept as a RAW string: the source is full of      #
# `\r?\n`, `\\` and `\b` sequences that a normal Python string would eat.       #
# --------------------------------------------------------------------------- #
RUN_CHECKS_JS = r"""'use strict';
/*
 * TAP runner for grahamearley/FirestoreGoogleAppsScript.
 *
 * The repo has no executable test suite -- Tests.ts is GSUnit, driven by the
 * Google Apps Script runtime against a live Firestore, and package.json has no
 * `test` script -- so the checks are the repo's own `npm run lint`, split per
 * file:
 *
 *     tsc::<path>       the file type-checks under some tsconfig in the repo
 *     eslint::<path>    the file lints clean under the repo's own eslintConfig
 *
 * The tool comes FIRST in the id on purpose; see the Python module docstring.
 *
 * Every stage runs this same script. What differs between stages is only the
 * content of the worktree, never the command.
 */

const fs = require('fs');
const path = require('path');
const cp = require('child_process');

const REPO = (process.argv[2] || process.cwd()).replace(/\\/g, '/').replace(/\/+$/, '');
process.chdir(REPO);

const SKIP_DIRS = new Set(['node_modules', '.git']);
const MAX_BUFFER = 1 << 28;

// The repo's prettier config asks for CRLF, but this dataset's patches were
// captured LF-normalised, so the reconstructed worktree can only ever be LF and
// `prettier/prettier` would report one "Insert CR" per line in EVERY stage.
// This overrides that one option and nothing else -- every other prettier
// setting and every other ESLint rule stays exactly as the repo configured it.
const ENDOFLINE_OVERRIDE = '{"prettier/prettier":["error",{"endOfLine":"auto"}]}';

// Diagnostics are printed as TAP comments at the end. Every line is prefixed
// with "# " so a tool that happens to emit "ok " or "not ok " can never be
// mistaken for a result line by parse_log.
const notes = [];
function note(line) {
  String(line).split(/\r?\n/).forEach(function (l) {
    notes.push(l);
  });
}

function walk(dir, out) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return out;
  }
  entries.sort(function (a, b) {
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });
  for (const e of entries) {
    if (SKIP_DIRS.has(e.name)) continue;
    // node_modules is a symlink into the build-time dependency tree. Never
    // descend through it, and never report a linked file as if the repo owned it.
    if (e.isSymbolicLink()) continue;
    const p = dir === '.' ? e.name : dir + '/' + e.name;
    if (e.isDirectory()) walk(p, out);
    else if (e.isFile()) out.push(p);
  }
  return out;
}

function rel(p) {
  let s = String(p).replace(/\\/g, '/').trim();
  if (s.startsWith(REPO + '/')) s = s.slice(REPO.length + 1);
  if (s.startsWith('./')) s = s.slice(2);
  return s;
}

const allFiles = walk('.', []);
const tsFiles = allFiles.filter(function (f) { return f.endsWith('.ts'); }).sort();
const tsconfigs = allFiles.filter(function (f) { return path.basename(f) === 'tsconfig.json'; }).sort();

note('repo=' + REPO);
note('discovered ' + tsFiles.length + ' .ts file(s), ' + tsconfigs.length + ' tsconfig(s)');

/* ---------------------------------------------------------------- tsc ---- */

const TSC = 'node_modules/typescript/bin/tsc';
let tscUsable = true;
let tscNote = '';
const tscErrors = new Set();  // repo files carrying at least one error
const tscChecked = new Set(); // repo files actually pulled into a program

if (tsconfigs.length === 0) {
  tscUsable = false;
  tscNote = 'no tsconfig.json in the worktree';
} else if (!fs.existsSync(TSC)) {
  tscUsable = false;
  tscNote = 'typescript is not installed';
}

if (tscUsable) {
  for (const cfg of tsconfigs) {
    const r = cp.spawnSync(
      process.execPath,
      [TSC, '-p', cfg, '--noEmit', '--pretty', 'false', '--listFiles'],
      { encoding: 'utf8', maxBuffer: MAX_BUFFER }
    );
    if (r.error) {
      tscUsable = false;
      tscNote = 'tsc failed to start: ' + r.error.message;
      break;
    }
    note('tsc -p ' + cfg + ' exit=' + r.status);
    const out = (r.stdout || '') + '\n' + (r.stderr || '');
    for (let line of out.split(/\r?\n/)) {
      line = line.trim();
      if (!line) continue;

      const diag = /^(.+?)\((\d+),(\d+)\):\s+error\s+TS\d+/.exec(line);
      if (diag) {
        const f = rel(diag[1]);
        // An error inside a dependency's own .d.ts is not this repo's file and
        // must not fail a repo file's id. Still surfaced as a comment.
        if (f.startsWith('node_modules/') || f.startsWith('/')) note('  dep: ' + line);
        else {
          tscErrors.add(f);
          note('  ' + line);
        }
        continue;
      }

      // A config-level diagnostic carries no file, so nothing can be attributed
      // and the whole run is untrustworthy: fail every id rather than report a
      // never-checked file as clean.
      if (/^error\s+TS\d+/.test(line)) {
        tscUsable = false;
        tscNote = line;
        note('  ' + line);
        continue;
      }

      // --listFiles output: a bare absolute path, no "(line,col):" anywhere.
      if (line.startsWith('/') && line.endsWith('.ts')) {
        const f = rel(line);
        if (!f.startsWith('/') && !f.startsWith('node_modules/')) tscChecked.add(f);
      }
    }
    if (!tscUsable) break;
  }
}
if (!tscUsable) note('tsc unusable: ' + tscNote);

/* ------------------------------------------------------------- eslint ---- */

const ESLINT = 'node_modules/eslint/bin/eslint.js';
let eslintUsable = fs.existsSync(ESLINT);
let eslintNote = eslintUsable ? '' : 'eslint is not installed';
const eslintErrors = new Map(); // repo file -> error count

if (eslintUsable) {
  const r = cp.spawnSync(
    process.execPath,
    [ESLINT, '--no-color', '-f', 'json', '--ext', '.ts', '--rule', ENDOFLINE_OVERRIDE, '.'],
    { encoding: 'utf8', maxBuffer: MAX_BUFFER }
  );
  note('eslint exit=' + r.status);
  let parsed = null;
  try {
    parsed = JSON.parse(r.stdout);
  } catch (e) {
    parsed = null;
  }
  if (!Array.isArray(parsed)) {
    // eslint exits 2 with an empty stdout when it cannot find a configuration --
    // exactly the baseline and test-patch state of this repo, where package.json
    // still carries the old `standard` block and no eslintConfig.
    eslintUsable = false;
    const first = (r.stderr || r.stdout || 'no JSON report').split(/\r?\n/).filter(function (l) {
      return l.trim();
    })[0];
    eslintNote = (first || 'no JSON report').trim();
    note('eslint unusable: ' + eslintNote);
  } else {
    for (const res of parsed) {
      const f = rel(res.filePath);
      const count = (res.errorCount || 0) + (res.fatalErrorCount || 0);
      eslintErrors.set(f, count);
      if (count > 0) {
        note('  ' + f + ': ' + count + ' error(s)');
        for (const m of (res.messages || []).slice(0, 3)) {
          note('      ' + (m.ruleId || 'fatal') + ' ' + m.line + ':' + m.column + ' ' + m.message);
        }
      }
    }
  }
}

/* ---------------------------------------------------------------- TAP ---- */

const results = [];
for (const f of tsFiles) {
  if (!tscUsable) results.push(['not ok', 'tsc::' + f, tscNote]);
  else if (tscErrors.has(f)) results.push(['not ok', 'tsc::' + f, 'type error(s)']);
  else if (tscChecked.has(f)) results.push(['ok', 'tsc::' + f, '']);
  else results.push(['skip', 'tsc::' + f, 'not part of any tsconfig program']);

  if (!eslintUsable) results.push(['not ok', 'eslint::' + f, eslintNote]);
  else if (!eslintErrors.has(f)) results.push(['skip', 'eslint::' + f, 'not linted']);
  else if (eslintErrors.get(f) > 0) results.push(['not ok', 'eslint::' + f, eslintErrors.get(f) + ' error(s)']);
  else results.push(['ok', 'eslint::' + f, '']);
}

const out = [];
out.push('1..' + results.length);
results.forEach(function (r, i) {
  const num = i + 1;
  if (r[0] === 'skip') out.push('ok ' + num + ' ' + r[1] + ' # SKIP ' + r[2]);
  else {
    // The reason goes on its own comment line, never appended to the name:
    // parse_log takes everything after the number as the test id.
    out.push(r[0] + ' ' + num + ' ' + r[1]);
    if (r[0] === 'not ok' && r[2]) out.push('# reason: ' + r[2]);
  }
});
out.push('# tests ' + results.length);
notes.forEach(function (l) { out.push('# ' + l); });
process.stdout.write(out.join('\n') + '\n');
"""


# --------------------------------------------------------------------------- #
# Shell scripts.                                                              #
#                                                                             #
# `__REPO__` is substituted with str.replace rather than str.format/f-strings   #
# so that `${BASE_COMMIT}`, `$(git …)` and `{` need no escaping and cannot be   #
# silently mangled. None of these use backslash line-continuations either.      #
# --------------------------------------------------------------------------- #

CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

# Force a CONTENT comparison instead of trusting the index's stat cache.
#
# Without this the check can pass on a file whose bytes differ from its blob,
# as long as the cached size and mtime still match -- which is exactly how the
# stale-CRLF .github/CODE_OF_CONDUCT.md described in the module docstring got
# through a clean build and only showed up in the delivered image. Exit status
# is ignored on purpose: a genuinely modified file makes this return 1, and
# reporting it is `git status`'s job below.
git update-index -q --really-refresh > /dev/null 2>&1 || true

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

LINK_NODE_MODULES_SH = """#!/bin/bash
# Test time only, never build time.
#
# The dependency tree is installed at /home/npmdeps because at the BASE commit
# `node_modules` is not in .gitignore -- the fix patch adds it -- so a real
# directory in the worktree would leave `git status --porcelain` dirty in the
# shipped image. Linking it in here keeps the delivered container clean and
# still gives eslint and tsc the module resolution they expect: node resolves
# through the symlink by realpath, and eslint looks for its plugins in
# <repo>/node_modules, which is exactly what this creates.
set -e

cd /home/__REPO__

if [ ! -e node_modules ]; then
  ln -s __NPM_DEPS_DIR__/node_modules node_modules
fi

test -f node_modules/typescript/bin/tsc
test -f node_modules/eslint/bin/eslint.js
"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
bash /home/check_git_changes.sh
git checkout "${BASE_COMMIT}"
bash /home/check_git_changes.sh

# Build-time validation of exactly the two `git apply` invocations the test
# stages will run, so a patch that cannot apply fails the BUILD rather than
# surfacing later as an empty test result that reads like a broken repo.
git apply --check --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# With both patches applied this is the post-fix dependency manifest. Lift it
# out and install it once, here, instead of hitting the registry in every test
# stage: the base commit declares no dependencies at all, so there is nothing
# to install from the pristine tree.
mkdir -p __NPM_DEPS_DIR__
cp package.json package-lock.json __NPM_DEPS_DIR__/

# -x is NOT optional. `appsscript.json` is gitignored at the base commit and
# added by the fix patch; a plain `git clean -fd` leaves it on disk, `git status`
# still reports clean because ignored files are not listed, and the next
# `git apply` dies with "already exists in working directory".
git checkout -- .
git clean -fdxq
bash /home/check_git_changes.sh

cd __NPM_DEPS_DIR__

# --unsafe-perm is NOT optional either. npm 6 refuses to run lifecycle scripts
# as root and only WARNS about it, so without the flag the repo's own
# `postinstall` is skipped silently and `tsc` then fails with
#   node_modules/@types/node/globals.d.ts(144,13): error TS2403 ... 'console'
# because the duplicate `declare var console` in @types/google-apps-script is
# still live.
npm ci --unsafe-perm --no-audit --no-fund

# Assert the postinstall actually ran rather than trusting the exit status --
# the failure mode above is a WARNING, not an error.
grep -q '^//declare var console' \\
    node_modules/@types/google-apps-script/google-apps-script.base.d.ts
test -f node_modules/typescript/bin/tsc
test -f node_modules/eslint/bin/eslint.js

# The worktree must still be pristine: nothing above was allowed to touch it.
cd /home/__REPO__
bash /home/check_git_changes.sh
"""

RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

bash /home/link-node-modules.sh

__CHECK_CMD__ 2>&1 || true
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

bash /home/link-node-modules.sh

__CHECK_CMD__ 2>&1 || true
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

bash /home/link-node-modules.sh

__CHECK_CMD__ 2>&1 || true
"""


def _render(template: str, repo: str) -> str:
    return (
        template.replace("__CHECK_CMD__", CHECK_CMD)
        .replace("__NPM_DEPS_DIR__", NPM_DEPS_DIR)
        .replace("__REPO__", repo)
    )


class FirestoreGoogleAppsScriptImageBase(Image):
    """Per-PR base: clone, pin to BASE_COMMIT, run the COMPLETE history scrub.

    Carries no staged files — neither patch has a binary section, so there is
    nothing to lift out of the object database before the prune.
    """

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
        return NODE_IMAGE

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # The COMPLETE scrub -- gc, repack and all four integrity asserts -- lives
        # here and only here, so `pr-<N>` has no scrub block at all.
        # `Image._HARDENING_BLOCK` is used verbatim rather than a hand-rolled
        # variant so the asserts can never quietly diverge from the harness's own
        # definition; it already carries the submodule pass as its second RUN.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere and cannot drift.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file:
        #
        #     if cls.SYNTAX_DIRECTIVE in raw: return raw     (image.py:316-317)
        #
        # and the directive has to stay. Dropping it to re-enable the enhancer
        # would let _standardize_repo_fetch rewrite the clone and append a SECOND
        # copy of the hardening block.
        #
        # No apt-get here: node:14-bullseye is buildpack-deps based and already
        # ships git and ca-certificates (confirmed by cloning inside it), and the
        # TAP runner is JavaScript precisely so that python3 is never needed.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            # Re-materialise every worktree file from the index, unconditionally.
            #
            # `git clone` checks out the DEFAULT branch first, and this repo's
            # master carries a .gitattributes with `text eol=crlf`, so
            # .github/CODE_OF_CONDUCT.md lands as CRLF. The base commit has no
            # .gitattributes at all, but the file's blob sha is identical at both
            # commits, so `git checkout ${BASE_COMMIT}` sees no change and leaves
            # the converted bytes in place. The result is a worktree file that
            # differs from its own blob -- and the index's cached size/mtime still
            # match it, so `git status` reports clean until something forces a
            # content comparison.
            #
            # Reproduced with nothing but `git clone` + `git checkout <base>` in a
            # stock node image, so this is upstream behaviour, not the harness's.
            # `git rm --cached -r .` + `git reset --hard` is git's own documented
            # recipe for this: dropping every index entry and resetting rebuilds
            # both the worktree AND the index stat cache from HEAD, under the
            # attributes that actually apply here (none). `git checkout-index -a
            # -f` alone is NOT enough — it corrects the bytes but leaves the stale
            # stat info behind, so `git status` keeps reporting the file modified
            # even once its content matches its blob exactly.
            #
            # `reset --hard` rather than `add --renormalize` because reset
            # DISCARDS a real difference where renormalize would silently STAGE
            # it, quietly turning genuine dirt into a clean-looking tree.
            #
            # The assert makes a regression fail the build rather than ship, and
            # --really-refresh forces a content comparison so it cannot pass on
            # stat-cache luck — which is exactly how this got through the first
            # build and only surfaced in the delivered image.
            (
                "RUN set -eux; \\\n"
                "    git rm --cached -r -q . > /dev/null; \\\n"
                "    git reset --hard; \\\n"
                "    git update-index -q --really-refresh || true; \\\n"
                '    test -z "$(git status --porcelain)"; \\\n'
                '    test -z "$(git ls-files --eol | grep w/crlf || true)"'
            ),
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class FirestoreGoogleAppsScriptImageDefault(Image):
    """Per-PR image: stage the patches and run scripts, provision dependencies.

    Carries no history scrub — `base-pr-<N>` already ran the complete one.
    """

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
        return FirestoreGoogleAppsScriptImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run-checks.js", RUN_CHECKS_JS),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "link-node-modules.sh", _render(LINK_NODE_MODULES_SH, repo)),
            File(".", "prepare.sh", _render(PREPARE_SH, repo)),
            File(".", "run.sh", _render(RUN_SH, repo)),
            File(".", "test-run.sh", _render(TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", _render(FIX_RUN_SH, repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # One COPY per file, matching the reference PR Dockerfile line for line.
        copy_command = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- the base is pinned to this PR's base commit and has already run
        # the full scrub (gc, repack, all four asserts), so there is nothing left
        # to prune here. Repeating it would only re-run an expensive no-op.
        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("grahamearley", "FirestoreGoogleAppsScript")
class GrahamearleyFirestoreGoogleAppsScript(Instance):
    """Harness instance for grahamearley/FirestoreGoogleAppsScript — TAP output."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FirestoreGoogleAppsScriptImageDefault(self.pr, self._config)

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
        """Parse the TAP stream emitted by /home/run-checks.js.

            1..34
            ok 1 tsc::Auth.ts
            not ok 2 eslint::Auth.ts
            # reason: 75 error(s)
            ok 3 tsc::Query.ts # SKIP not part of any tsconfig program
            # tests 34

        The id is `<tool>::<path>` and is identical across all three stages for
        any file present in that stage, which is what keeps the f2p/p2p set
        comparison meaningful. Reasons are always on their own `#` comment line,
        never appended to the name, because everything after the test number is
        the id.

        A stage where the file does not exist yet simply emits no line for it,
        which the harness reads as NONE.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_ok = re.compile(r"^ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = re_not_ok.match(line)
            if m:
                name = re_directive.sub("", m.group(1)).strip()
                if name:
                    failed_tests.add(name)
                continue

            m = re_ok.match(line)
            if m:
                raw_name = m.group(1)
                is_skip = bool(re_directive.search(raw_name))
                name = re_directive.sub("", raw_name).strip()
                if not name:
                    continue
                if is_skip:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        # A name that failed anywhere wins over the same name passing elsewhere,
        # so a duplicate can never look green by accident. Ids are unique per
        # stage by construction, so this is a guard rather than a fixup.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
