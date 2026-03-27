import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _clean_test_name(name: str) -> str:
    """Strip variable timing and metadata from test names for stable eval matching."""
    # Strip vitest file-level metadata: (2 tests) 75ms, (1 test | 1 failed) 120ms
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    # Strip parenthesized timing: (75ms), (150 ms), (8.954 s)
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


class InquirerImageBase(Image):
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

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y git make python3
RUN corepack enable
{code}

{self.clear_env}

"""


class InquirerImageDefault(Image):
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
        return InquirerImageBase(self.pr, self._config)

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

corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

# Detect test framework and run accordingly
if [ -f "vitest.config.ts" ] || [ -f "vitest.config.mts" ] || [ -f "vitest.config.js" ]; then
    yarn vitest run --reporter=verbose; exit 0
elif [ -f "packages/inquirer/test" ] || grep -q '"mocha"' package.json 2>/dev/null; then
    yarn test || true; exit 0
else
    # Fallback: try yarn test (covers both eras)
    yarn test || true; exit 0
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

if [ -f "vitest.config.ts" ] || [ -f "vitest.config.mts" ] || [ -f "vitest.config.js" ]; then
    yarn vitest run --reporter=verbose; exit 0
elif [ -f "packages/inquirer/test" ] || grep -q '"mocha"' package.json 2>/dev/null; then
    yarn test || true; exit 0
else
    yarn test || true; exit 0
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
corepack enable || true
YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install || true

if [ -f "vitest.config.ts" ] || [ -f "vitest.config.mts" ] || [ -f "vitest.config.js" ]; then
    yarn vitest run --reporter=verbose; exit 0
elif [ -f "packages/inquirer/test" ] || grep -q '"mocha"' package.json 2>/dev/null; then
    yarn test || true; exit 0
else
    yarn test || true; exit 0
fi

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("SBoudrias", "Inquirer.js")
class InquirerJs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return InquirerImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # === Vitest file-level PASS ===
            m = re.match(r"PASS\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                passed_tests.add(name)
                continue

            # === Vitest file-level FAIL ===
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                failed_tests.add(name)
                continue

            # === Vitest/Jest test-level pass (✓/✔) ===
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                passed_tests.add(name)
                continue

            # === Vitest/Jest test-level fail (×/✕/✗) ===
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                failed_tests.add(name)
                continue

            # === Vitest skipped (↓/○) ===
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                skipped_tests.add(name)
                continue

            # === Mocha passing (N passing) — informational only ===

            # === Mocha individual test pass: ✓ name (Nms) ===
            m = re.match(r"✓\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if m:
                name = m.group(1).strip()
                passed_tests.add(name)
                continue

            # === Mocha individual test fail: N) test name ===
            m = re.match(r"\d+\)\s+(.+)$", stripped)
            if m:
                name = m.group(1).strip()
                # Only count as fail if it looks like a test name (not a stack trace or number)
                if name and not name.startswith("at ") and not name.startswith("Error"):
                    failed_tests.add(name)
                continue

            # === Mocha pending/skipped: - name ===
            m = re.match(r"-\s+(.+)$", stripped)
            if m:
                name = m.group(1).strip()
                if name and name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                continue

        # If a test appears in both pass and fail, it failed
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
