import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class WebextReduxImageBase(Image):
    """Repo-level base (`images/base/`, tag `:base`).

    Deliberately does NOT override `dockerfile()`: the default in
    `Image.dockerfile()` (harness/image.py:200) is already the
    canonical FROM + apt + `git clone ${REPO_URL}` + `git checkout
    ${BASE_COMMIT}` + hardening sequence we want. Overriding here
    would only invite drift from the framework contract.
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

    def dependency(self) -> str:
        # node:14-bullseye pinned deliberately for the eslint 4.x /
        # mocha 5.x / babel 7.x combo declared in package.json at
        # base.sha:
        #   * eslint 4.18.2 was released before Node 15's removal of
        #     several deprecated fs/util internals; it throws
        #     `TypeError [ERR_INVALID_CALLBACK]` on Node >= 16 when
        #     linting some ES2020+ syntax.
        #   * mocha 5.2.0 predates the `experimentalModules` gate on
        #     Node >= 12, but its `--require @babel/register` path
        #     works fine on 14.
        #   * `bullseye` (Debian 11) is used over `-slim` because
        #     several transitive devDeps (rollup terser plugin's
        #     source-map-support fallback, sinon's diff pretty-printer)
        #     lazily require gyp-compiled modules that need python3 +
        #     make + g++ from the full image. The default apt package
        #     list already includes build-essential + python3 + make,
        #     so gyp builds resolve cleanly.
        return "node:14-bullseye"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "Image":
        # Chain to the repo-level base image so `images/base/` is
        # emitted alongside `images/pr-<N>/` (mirroring the layout
        # used by hakimel/reveal.js and every other multi-image
        # config). The base carries the OS toolchain + `git clone`;
        # this PR image only layers per-PR patches and runs
        # prepare.sh (which does the per-PR `git checkout
        # <base.sha>` + `npm ci`).
        return WebextReduxImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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
  echo "ERROR: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "ERROR: Working directory is not clean"
  exit 1
fi

echo "Git repository is clean"
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git reset --hard
git checkout {pr.base.sha}

# `npm ci` is preferred over `npm install` for the base image build:
# package-lock.json is committed at base.sha and matches package.json
# exactly, so ci gives a deterministic, faster install than a fresh
# resolve. --prefer-offline lets docker layer-caching reuse the npm
# cache dir between rebuilds. --no-audit / --no-fund cut noise and a
# couple of network calls that would otherwise slow the layer.
npm ci --prefer-offline --no-audit --no-fund
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Baseline test run: no patches applied.
#
# Mocha invocation notes:
#   * `--require @babel/register` transpiles src/ ES modules on
#     import (the tests do `import ... from '../src/...'`).
#     `npm run pretest` (babel src --out-dir lib) is NOT required
#     for the mocha run itself because @babel/register handles it
#     at load time; skipping pretest saves ~5s per stage.
#   * `--recursive` walks test/ (mocha's default `--dir`) so any
#     nested test files are picked up.
#   * `--reporter json` writes a single top-level JSON object at
#     the END of the run to stdout. We redirect stdout to a file
#     so parse_log has a clean, parseable capture. Stderr is
#     diverted separately so babel-register deprecation warnings
#     do not corrupt the JSON.
#   * `|| true` because mocha exits non-zero on any failing test;
#     the JSON reporter still writes its full report before exit,
#     so parse_log's counts are complete even on failures.
rm -f /tmp/mocha-results.json /tmp/mocha-stderr.log
node_modules/.bin/mocha --require @babel/register --recursive \\
    --reporter json \\
    > /tmp/mocha-results.json 2>/tmp/mocha-stderr.log || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  # Empty-but-valid Mocha-shaped JSON keeps parse_log's walker
  # happy in the (rare) case mocha crashes before writing.
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply only test.patch. It REWRITES the 4 files under test/
# (Store.test.js, applyMiddleware.test.js, listener.test.js,
# wrapStore.test.js) to exercise the MV3 API (sendMessage-based
# proxy) that fix.patch introduces. Since fix.patch is NOT
# applied here, src/ still exposes the pre-MV3 port-based API,
# so the rewritten tests are expected to FAIL en masse — that's
# exactly the discrimination signal the harness needs.
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# No `npm install` needed: test.patch does not touch package.json.
# No pretest / rebuild needed: @babel/register transpiles on load.
# See run.sh for the mocha invocation rationale (stdout capture,
# stderr divert, fallback JSON).
rm -f /tmp/mocha-results.json /tmp/mocha-stderr.log
node_modules/.bin/mocha --require @babel/register --recursive \\
    --reporter json \\
    > /tmp/mocha-results.json 2>/tmp/mocha-stderr.log || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply fix.patch first, then test.patch. Order is conventional
# (matches every other repo config in this harness); the two touch
# disjoint files here (fix→src/**/*.js + package.json + index.d.ts,
# test→test/*.test.js) so order is not load-bearing.
#
# fix.patch touches package-lock.json (the version bump 2.1.9 →
# 3.0.0-mv3.0 propagates through lockfileVersion metadata). The
# lock hunks routinely reject against the lock npm regenerated
# during prepare.sh — that's the same drift class fixed on lokus
# (see lokus_ai/lokus.py fix-run.sh). Reject fallback + .rej
# cleanup handles it; no `npm install` re-run needed because
# fix.patch does NOT change dependencies (only the version field).
git apply --whitespace=nowarn /home/fix.patch || {{
    echo "Warning: fix.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/fix.patch || true
    find . -name '*.rej' -delete
}}
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# See run.sh for the mocha invocation rationale.
rm -f /tmp/mocha-results.json /tmp/mocha-stderr.log
node_modules/.bin/mocha --require @babel/register --recursive \\
    --reporter json \\
    > /tmp/mocha-results.json 2>/tmp/mocha-stderr.log || true

echo '===MOCHA_JSON_BEGIN==='
if [[ -s /tmp/mocha-results.json ]]; then
  cat /tmp/mocha-results.json
else
  echo '{{"stats":{{"tests":0,"passes":0,"failures":0,"pending":0}},"passes":[],"failures":[],"pending":[]}}'
fi
echo
echo '===MOCHA_JSON_END==='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("tshaddix", "webext-redux")
class WebextRedux(Instance):
    """Config for tshaddix/webext-redux PR-297 (Manifest V3 rework).

    Registry key: "tshaddix/webext-redux" (matches pr.org/pr.repo
    verbatim — dashes preserved on the repo side per the convention
    in b1). Folder name uses an underscore (`webext_redux.py`)
    because Python identifiers can't contain dashes.

    parse_log strategy: brace-depth-tracking JSON extractor with
    string-state awareness (same walker as
    javascript/microsoft/monaco_editor.py and
    javascript/lokus_ai/lokus.py — see either for the algorithm's
    correctness proof against the 7 pathological cases). Schema is
    Mocha's `--reporter json` single-top-level object with
    `passes[]`, `failures[]`, `pending[]` arrays of
    `{title, fullTitle, ...}` records (same as monaco_editor.py's
    extraction step).
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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

        # Strip ANSI escapes defensively; Mocha's JSON reporter does
        # not emit colour codes, but wrapping tooling (harness log
        # capture, docker layer, tee, etc.) has been observed to
        # re-inject them.
        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        cleaned_log = ansi_escape.sub("", test_log)

        # Balanced-JSON walker. Tracks brace depth OUTSIDE strings
        # only; string state is tracked so `{` / `}` inside test
        # titles cannot confuse the depth counter, and backslash-
        # escapes inside strings are honoured so `\"` doesn't close
        # a string prematurely. On EOF-without-close, advance ONE
        # character past the stray `{` and keep scanning — a single
        # unbalanced brace in prose output must not swallow every
        # subsequent real JSON block. See monaco_editor.py /
        # lokus.py for the same walker plus its 7-scenario test
        # suite.
        json_blocks: list[str] = []
        n = len(cleaned_log)
        i = 0
        while i < n:
            if cleaned_log[i] != "{":
                i += 1
                continue
            depth = 0
            in_string = False
            escape = False
            end = -1
            j = i
            while j < n:
                ch = cleaned_log[j]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                j += 1
            if end >= 0:
                json_blocks.append(cleaned_log[i : end + 1])
                i = end + 1
            else:
                i += 1

        def _title_of(entry: dict) -> str:
            # Mocha reports both `title` (leaf) and `fullTitle`
            # (parent-suite-prefixed). Prefer fullTitle for
            # uniqueness across suites.
            if not isinstance(entry, dict):
                return ""
            full = entry.get("fullTitle")
            if isinstance(full, str) and full:
                return full
            leaf = entry.get("title")
            return leaf if isinstance(leaf, str) else ""

        for block in json_blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            # Mocha --reporter json top-level schema:
            #   {"stats": {...},
            #    "tests":    [{...}],   -- ALL run tests (union of
            #                              passes, failures, pending)
            #    "pending":  [{...}],   -- skipped/pending
            #    "failures": [{...}],   -- errored
            #    "passes":   [{...}]}   -- succeeded
            # We iterate passes/failures/pending directly. `tests`
            # is redundant with the other three combined.
            for entry in data.get("passes", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                # Passing does not overrule an earlier failure of
                # the same fullTitle (matches mocha's own semantics
                # when a retry passes but the original run failed).
                if name not in failed_tests:
                    passed_tests.add(name)

            for entry in data.get("failures", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                failed_tests.add(name)
                # Failure always wins over an earlier "passed".
                passed_tests.discard(name)

            for entry in data.get("pending", []) or []:
                name = _title_of(entry)
                if not name:
                    continue
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
