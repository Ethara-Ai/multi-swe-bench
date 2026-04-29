import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.test_result import TestResult
from multi_swe_bench.harness.pull_request import PullRequest



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
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
set -e
cd /home/[[REPO_NAME]]
###ACTION_DELIMITER###
if [ "$(uname -m)" = "aarch64" ]; then pip install uv && uv sync --extra test; else pip install -e '.[test]'; fi
###ACTION_DELIMITER###
if [ "$(uname -m)" = "aarch64" ]; then echo 'uv run pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""' > test_commands.sh; else echo 'python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""' > test_commands.sh; fi
###ACTION_DELIMITER###
cat test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if [ "$(uname -m)" = "aarch64" ]; then uv run pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; else python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

apply_one_patch() {
    local p="$1"
    # Remove files the patch marks as "new file" that already exist in worktree
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn "$p" 2>/dev/null && return 0
    # Restore for --3way retry
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn --3way "$p" 2>/dev/null && return 0
    # Last resort: --reject applies what it can, skip broken hunks
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn --reject "$p" 2>/dev/null || true
    find . -name '*.rej' -delete 2>/dev/null
    return 0
}

apply_one_patch /home/test.patch || true
if [ "$(uname -m)" = "aarch64" ]; then uv sync --extra test --quiet 2>/dev/null; fi
if [ "$(uname -m)" = "aarch64" ]; then uv run pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; else python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]

apply_one_patch() {
    local p="$1"
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn "$p" 2>/dev/null && return 0
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn --3way "$p" 2>/dev/null && return 0
    # Last resort: --reject applies what it can, skip broken hunks
    awk '/^diff --git/{f=$NF; sub(/^b\\//,"",f)} /^new file mode/{print f}' "$p" | while read -r nf; do
        [ -f "$nf" ] && rm -f "$nf"
    done
    git apply --whitespace=nowarn --reject "$p" 2>/dev/null || true
    find . -name '*.rej' -delete 2>/dev/null
    return 0
}

# Apply fix FIRST (provides modules test files depend on), then test patch
apply_one_patch /home/fix.patch || true
apply_one_patch /home/test.patch || true
if [ "$(uname -m)" = "aarch64" ]; then uv sync --extra test --quiet 2>/dev/null; fi
if [ "$(uname -m)" = "aarch64" ]; then uv run pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; else python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""; fi

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git

RUN if [ ! -f /bin/bash ]; then \
        if command -v apk >/dev/null 2>&1; then \
            apk add --no-cache bash; \
        elif command -v apt-get >/dev/null 2>&1; then \
            apt-get update && apt-get install -y --no-install-recommends bash; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y bash; \
        else \
            exit 1; \
        fi \
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/github/spec-kit.git /home/spec-kit

WORKDIR /home/spec-kit
RUN git reset --hard
RUN git checkout {pr.base.sha}

ARG TARGETARCH
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        pip install uv && uv sync --extra test; \
    else \
        pip install -e '.[test]'; \
    fi
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("github", "spec-kit")
class SpecKit(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        passed_pattern = re.compile(r"PASSED\s+(tests/.*?)(?:\s|$)", re.MULTILINE)
        for test in passed_pattern.findall(test_log):
            passed_tests.add(test.strip())

        failed_pattern = re.compile(
            r"FAILED\s+(tests/.*?)(?:\s| - .*)?$", re.MULTILINE
        )
        for test in failed_pattern.findall(test_log):
            failed_tests.add(test.strip())

        skipped_pattern = re.compile(
            r"^\s*\[\s*\d+\s*\]\s+SKIPPED\s*\[\d+\]\s*(.*?):", re.MULTILINE
        )
        for test in skipped_pattern.findall(test_log):
            skipped_tests.add(test.strip())

        xfail_pattern = re.compile(
            r"^\s*\[\s*\d+\s*\]\s+XFAIL\s+(.*)\s*$", re.MULTILINE
        )
        for test in xfail_pattern.findall(test_log):
            failed_tests.add(test.strip())

        error_pattern = re.compile(
            r"ERROR\s+(tests/.*?)(?:\s|$)", re.MULTILINE
        )
        for test in error_pattern.findall(test_log):
            failed_tests.add(test.strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
