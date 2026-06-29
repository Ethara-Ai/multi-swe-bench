import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.9-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        if self._config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM python:3.9-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} base Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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
set -eux

cd /home/{pr.repo}
git reset --hard
git checkout --detach {pr.base.sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "{pr.base.sha}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD
        git remote remove origin 2>/dev/null || true
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
        git reflog expire --expire=now --all
        git reflog expire --expire-unreachable=now --all
        git gc --prune=now --aggressive
        rm -f .git/objects/info/alternates
    '
fi
pip install poetry
poetry lock || true
poetry install --with dev
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
poetry run pytest -v --no-header -rA --tb=no -p no:cacheprovider tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
# Strip binary hunks from patches before applying
python3 -c "
import re, sys
for f in sys.argv[1:]:
    c = open(f).read()
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)GIT binary patch.*?(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)Binary files[^\\n]*differ\\n?(?:(?:(?!diff --git).)*)(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    open(f, 'w').write(c)
" /home/fix.patch /home/test.patch
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
poetry run pytest -v --no-header -rA --tb=no -p no:cacheprovider tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/[[REPO_NAME]]
# Strip binary hunks from patches before applying
python3 -c "
import re, sys
for f in sys.argv[1:]:
    c = open(f).read()
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)GIT binary patch.*?(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    c = re.sub(r'diff --git[^\\n]*\\n(?:(?:(?!diff --git).)*)Binary files[^\\n]*differ\\n?(?:(?:(?!diff --git).)*)(?=diff --git|\\Z)', '', c, flags=re.DOTALL)
    open(f, 'w').write(c)
" /home/test.patch /home/fix.patch
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --exclude='*.lock' /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
poetry run pytest -v --no-header -rA --tb=no -p no:cacheprovider tests/

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{repo}

RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("litestar-org", "129")
class LITESTAR_569_TO_16(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        # Parse the log content and extract test execution results.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        # Pattern for passed tests: either "tests/... PASSED" or "PASSED tests/..."
        passed_pattern = re.compile(
            r"(tests/[^\s]+)\s+PASSED|PASSED\s+(tests/[^\s]+)", re.MULTILINE
        )
        # Pattern for failed tests: "FAILED tests/..."
        failed_pattern = re.compile(r"FAILED\s+(tests/[^\s]+)", re.MULTILINE)
        # Pattern for collection/runtime errors: "ERROR tests/..."
        error_pattern = re.compile(r"ERROR\s+(tests/[^\s]+)", re.MULTILINE)
        # Pattern for skipped tests: "SKIPPED [n] tests/...:"
        skipped_pattern = re.compile(r"SKIPPED.*?(tests/[^:]+:\d+):", re.MULTILINE)
        # Extract passed tests
        for match in passed_pattern.finditer(log):
            test_name = match.group(1) or match.group(2)
            if test_name:
                passed_tests.add(test_name.strip())
        # Extract failed tests
        for match in failed_pattern.finditer(log):
            test_name = match.group(1)
            if test_name:
                failed_tests.add(test_name.strip())
        for match in error_pattern.finditer(log):
            test_name = match.group(1)
            if test_name:
                failed_tests.add(test_name.strip())
        # Extract skipped tests
        for match in skipped_pattern.finditer(log):
            test_name = match.group(1)
            if test_name:
                skipped_tests.add(test_name.strip())
        passed_tests -= failed_tests
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
