import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


class ImageBase(Image):
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
        return "node:20-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        python3 \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Install JS dependencies. Uses `npm install` (not `npm ci`) because:
#   1. `npm ci` requires an exact package-lock/package.json match and
#      aborts on any drift; we want a permissive install so fix-run.sh
#      can add dompurify+validator later without invalidating the tree.
#   2. --legacy-peer-deps because React 19 is new (Aug 2025) and several
#      @radix-ui / @testing-library / @tiptap deps still list React
#      <=18 as their peer, which fails npm 10's strict peer resolver.
#   3. --no-audit --no-fund silences noisy output that has no effect
#      on tests but slows the build and clutters logs.
#   4. --ignore-scripts skips the tauri/vite install-time scripts that
#      would otherwise try to pull Rust toolchains we do not need for
#      Vitest (unit + integration tests run in jsdom, no Rust needed).
npm install --legacy-peer-deps --no-audit --no-fund --ignore-scripts

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
# Discovery scope note — the repo ships TWO vitest configs at base.sha:
#   vitest.config.ts  →  include: ['src/**/*.{{test,spec}}.{{js,jsx,ts,tsx}}']
#   vitest.config.js  →  include: ['tests/**/*.{{test,spec}}.{{js,jsx,ts,tsx}}',
#                                  'src/**/*.{{test,spec}}.{{js,jsx,ts,tsx}}']
# Vitest resolves the .ts config with higher priority, so the ACTIVE
# include glob is src-only and tests/ files are silently ignored. Neither
# test.patch nor fix.patch modifies either config. Deleting the .ts
# config here forces Vitest to fall through to the .js config, whose
# include covers both src/ and tests/. This is essential so that the
# security test files added by test.patch (under tests/unit/, tests/
# integration/) are discoverable in stages 2 and 3 — otherwise all three
# stages report identical counts and f2p_tests is always 0.
#
# Same rm command runs in test-run.sh and fix-run.sh so all three stages
# use the same discovery scope (apples-to-apples p2p / f2p comparison).
rm -f vitest.config.ts

# Vitest invocation notes:
#  * NO positional args. Passing directory names like `tests/unit` as
#    positional CLI args made Vitest 3.x's discovery pipeline return
#    zero files in earlier runs (silently, no error message).
#  * --exclude '**/tests/e2e/**' skips Playwright specs. They match the
#    include glob (*.spec.js) but crash on load because they
#    `import from '@playwright/test'`, whose runtime isn't active inside
#    Vitest. The leading `**/` prefix protects against Vitest resolving
#    paths as absolute in some versions.
#  * `|| true` because Vitest exits non-zero on any test failure; the
#    JSON reporter still writes /tmp/vitest.json in that case, and
#    parse_log needs it.
rm -f /tmp/vitest.json
node_modules/.bin/vitest run \\
    --exclude '**/tests/e2e/**' \\
    --reporter=json \\
    --outputFile=/tmp/vitest.json || true

echo '===VITEST_JSON_BEGIN==='
if [[ -s /tmp/vitest.json ]]; then
  cat /tmp/vitest.json
else
  # Emit an empty-but-valid Vitest-shaped JSON so parse_log has
  # something well-formed to walk (avoids ambiguity between "runner
  # crashed" and "test crashed").
  echo '{{"testResults": [], "numTotalTests": 0}}'
fi
echo
echo '===VITEST_JSON_END==='

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply only the test.patch (introduces the security test files but
# NOT the src/core/security/*.js implementation that they depend on;
# most of the added tests are expected to fail here — that's the
# whole point of the test-only baseline).
git apply --whitespace=nowarn /home/test.patch || {{
    echo "Warning: test.patch did not apply cleanly, using --reject fallback"
    git apply --reject --whitespace=nowarn /home/test.patch || true
    find . -name '*.rej' -delete
}}

# No `npm install` needed: test.patch does not touch package.json.
# See run.sh for the rationale behind the next two commands.
rm -f vitest.config.ts
rm -f /tmp/vitest.json
node_modules/.bin/vitest run \\
    --exclude '**/tests/e2e/**' \\
    --reporter=json \\
    --outputFile=/tmp/vitest.json || true

echo '===VITEST_JSON_BEGIN==='
if [[ -s /tmp/vitest.json ]]; then
  cat /tmp/vitest.json
else
  echo '{{"testResults": [], "numTotalTests": 0}}'
fi
echo
echo '===VITEST_JSON_END==='

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply the fix first (adds src/core/security/*.js and updates
# package.json with dompurify@^3.2.6 + validator@^13.15.15), then
# the tests. Order matters: applying test.patch first would leave
# hunks referencing files created by fix.patch's context.
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

# fix.patch added two new runtime dependencies (dompurify, validator)
# AND ships a package-lock.json snapshot. That lock snapshot drifts
# against the lock npm regenerates during prepare.sh (different
# transitive versions), so 2 out of the 5 package-lock.json hunks
# reject cleanly, leaving the lock in a mixed state. Delete it so npm
# resolves fresh from package.json — this bypasses the drift and
# guarantees a consistent install regardless of patch state.
rm -f package-lock.json
npm install --legacy-peer-deps --no-audit --no-fund --ignore-scripts

# See run.sh for the rationale behind the next two commands.
rm -f vitest.config.ts
rm -f /tmp/vitest.json
node_modules/.bin/vitest run \\
    --exclude '**/tests/e2e/**' \\
    --reporter=json \\
    --outputFile=/tmp/vitest.json || true

echo '===VITEST_JSON_BEGIN==='
if [[ -s /tmp/vitest.json ]]; then
  cat /tmp/vitest.json
else
  echo '{{"testResults": [], "numTotalTests": 0}}'
fi
echo
echo '===VITEST_JSON_END==='

""".format(pr=self.pr),
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


@Instance.register("lokus-ai", "lokus")
class Lokus(Instance):
    """
    lokus-ai/lokus — Tauri desktop notes editor (React 19 + TipTap +
    Tldraw). PR #26 introduces a security-hardening layer
    (DOMPurify-based sanitizer, validator-based input checks, tightened
    CSP). Tests are Vitest (unit + integration) under jsdom.

    Registry key: "lokus-ai/lokus" (dashes preserved on the org side
    because `Instance.create()` builds the key from `pr.org`/`pr.repo`
    verbatim).

    E2E scope note: the repo also contains Playwright e2e specs under
    tests/e2e/ but they require the actual Tauri runtime (they call
    the app's "Open Workspace" dialog which routes through
    @tauri-apps/plugin-fs). Running them under pure Playwright +
    Chromium — with no Tauri process — makes them time out identically
    on both baselines, providing zero fix-vs-no-fix signal. We
    therefore exclude tests/e2e/** and rely on unit + integration to
    discriminate. See run.sh / test-run.sh / fix-run.sh for the
    filter.

    parse_log follows the same brace-depth JSON extraction pattern as
    csc302-spring-2020/proj-FakeBirds and mochajs/mocha — walks the log
    tracking `{`/`}` nesting outside strings/escapes to pull balanced
    top-level JSON blocks, then decodes each one and dispatches by
    schema (Vitest has `testResults`; Playwright/Mocha have other
    marker keys but we never expect them here).
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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

        # Strip ANSI escapes defensively; Vitest usually skips them
        # under --reporter=json but some CI wrappers re-inject them.
        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        cleaned_log = ansi_escape.sub("", test_log)

        # Walk the log extracting balanced top-level JSON blocks. We
        # track brace depth OUTSIDE of strings only; string state is
        # tracked so a `{` or `}` inside a test title cannot confuse
        # the depth counter. Backslash-escapes inside strings are
        # skipped so `\"` doesn't prematurely close the string.
        #
        # Backtracking behaviour: if a `{` starts a block that runs to
        # EOF without closing (e.g. a stray `{` in prose output like
        # "unbalanced { in a log line"), we abandon that attempt,
        # advance by ONE character past the stray `{`, and continue
        # scanning. Without this, one stray `{` upstream would swallow
        # every subsequent real JSON block in the log.
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

        def _record(name: str, status: str) -> None:
            if not name:
                return
            if status in ("passed", "pass"):
                # Passing does not overrule an earlier failure of the
                # same test name (matches Vitest's own semantics when
                # a test file gets retried and the retry passes but
                # the original run failed).
                if name not in failed_tests:
                    passed_tests.add(name)
            elif status in ("failed", "fail"):
                failed_tests.add(name)
                # Failure always wins over an earlier "passed" for the
                # same fullName.
                passed_tests.discard(name)
            elif status in ("skipped", "pending", "todo"):
                if name not in failed_tests and name not in passed_tests:
                    skipped_tests.add(name)

        for block in json_blocks:
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            # Vitest --reporter=json schema:
            #   {"testResults": [
            #       {"assertionResults": [
            #           {"fullName": "...", "status": "passed|failed|skipped|pending|todo",
            #            "title": "...", "ancestorTitles": ["...", "..."]}
            #       ], "name": "<file>", "status": "passed|failed"}
            #   ], "numTotalTests": N, ...}
            test_results = data.get("testResults")
            if not isinstance(test_results, list):
                continue

            for file_result in test_results:
                if not isinstance(file_result, dict):
                    continue
                assertions = file_result.get("assertionResults")
                if not isinstance(assertions, list):
                    continue
                for a in assertions:
                    if not isinstance(a, dict):
                        continue
                    status = str(a.get("status", "")).lower()
                    name = a.get("fullName")
                    if not name:
                        title = a.get("title") or ""
                        ancestors = a.get("ancestorTitles") or []
                        if isinstance(ancestors, list):
                            parts = [str(p) for p in ancestors if p] + (
                                [str(title)] if title else []
                            )
                            name = " > ".join(parts)
                        else:
                            name = str(title)
                    _record(str(name), status)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
