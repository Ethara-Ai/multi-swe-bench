import re
from typing import Optional, Union

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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-ts-0"

    def workdir(self) -> str:
        return "base-ts-0"

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
ENV CI=true

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@9

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
        return ImageBase(self.pr, self.config)

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
