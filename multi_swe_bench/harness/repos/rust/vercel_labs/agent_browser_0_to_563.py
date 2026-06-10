import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Era: vercel-labs/agent-browser PRs <= 563 (pre "Rust native rewrite").
#
# At these base commits the daemon/library is TypeScript and the regression
# tests added by the dataset's test_patch are vitest specs (`src/**/*.test.ts`,
# `test/**/*.test.ts`).  The repo also carries a thin Rust `cli/` wrapper, but
# the test command exercised here is the TS suite.
#
# Discovery (Docker, host arch arm64, verified):
#   * Toolchain is constant across the whole era (PR 3 / v0.6.0 .. PR 563 /
#     v0.15.x): pnpm (pnpm-lock.yaml, no packageManager field), node 20,
#     vitest ^4, playwright/playwright-core ^1.57.
#   * `pnpm install` then `pnpm exec playwright install --with-deps chromium`
#     installs the bundled Chromium (works on amd64 *and* arm64, unlike
#     Chrome-for-Testing).  Browser specs (browser.test.ts etc.) pass headless.
#   * `pnpm exec vitest run --reporter=verbose` emits one line per test:
#       ` ✓ src/foo.test.ts > suite > name 1ms`   (pass)
#       ` × src/foo.test.ts > suite > name 3ms`    (fail)
#       ` ↓ src/foo.test.ts > suite > name`        (skipped/todo)
#   * Full suite verified green (481 passed | 17 skipped) and the PR
#     test_patch + fix_patch apply cleanly with `git apply --whitespace=nowarn`.
# ---------------------------------------------------------------------------


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

    def dependency(self) -> str:
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that
        # strips every other ref/commit so the fix can't be read out of git
        # history. DockerfileEnhancer then injects the proxy/cert infra and the
        # final sanitize pass. None of that fires when dockerfile() is
        # overridden, which is why the previous two-stage build bypassed it.
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. We install pnpm (needed by prepare.sh), stage the runtime
        # helper scripts + patches into /home/, and warm the pnpm install +
        # bundled Chromium. The copied files live outside /home/{repo}, so the
        # hardening pass (which only operates inside the git tree) leaves them
        # untouched.
        return (
            "ENV CI=true\n"
            "RUN npm install -g pnpm@9\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Warm the pnpm install + bundled Chromium at image-build time so the eval
# runs don't need network. The repo is already checked out at ${{BASE_COMMIT}}
# and hardened by Image.dockerfile(), so this script no longer performs any
# git checkout itself. Steps are allowed to fail (|| true) because their only
# purpose here is to populate node_modules + the browser cache; the real
# pass/fail signal comes from the run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true
pnpm install || true
# Bundled Chromium + its OS deps (works on amd64 and arm64).
pnpm exec playwright install --with-deps chromium || npx --yes playwright install --with-deps chromium || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch
pnpm exec vitest run --reporter=verbose

""".format(pr=self.pr),
            ),
        ]


@Instance.register("vercel-labs", "agent_browser_0_to_563")
class AGENT_BROWSER_0_TO_563(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        # vitest@4 verbose reporter, ANSI stripped (leading space trimmed):
        #   "✓ src/foo.test.ts > suite > name 1ms"           -> passed
        #   "↓ src/foo.test.ts > suite > name"               -> skipped/todo
        #   "FAIL  src/foo.test.ts > suite > name"           -> failed
        # Failures are NOT printed with an inline "×" by the verbose reporter;
        # they only appear in the trailing "Failed Tests" block prefixed with
        # "FAIL  " (and no duration).  "×"/"✗" are still accepted defensively.
        # The "<file>.test.ts > ..." shape (note the " > ") excludes the
        # file-level summary lines such as "✓ src/foo.test.ts (37 tests) 13ms".
        dur_re = re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$")
        body_re = r"(\S+\.(?:test|spec)\.tsx?\s+>\s+.+)$"
        pass_re = re.compile(r"^[✓]\s+" + body_re)
        skip_re = re.compile(r"^[↓·]\s+" + body_re)
        fail_re = re.compile(r"^(?:FAIL|FAILED|[×✗])\s+" + body_re)

        for raw in test_log.splitlines():
            clean = ansi_re.sub("", raw).strip()
            if not clean:
                continue

            m = fail_re.match(clean)
            if m:
                failed_tests.add(dur_re.sub("", m.group(1)).strip())
                continue
            m = pass_re.match(clean)
            if m:
                passed_tests.add(dur_re.sub("", m.group(1)).strip())
                continue
            m = skip_re.match(clean)
            if m:
                skipped_tests.add(dur_re.sub("", m.group(1)).strip())
                continue

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
