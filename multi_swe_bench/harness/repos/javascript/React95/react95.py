from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "/home/React95"

_FILTER_BINARY = r"""filter_binary() {
  awk '
    function flush() { if (section != "" && !isbin) printf "%s", section }
    /^diff --git / { flush(); section=""; isbin=0 }
    /^GIT binary patch$/ { isbin=1 }
    /^Binary files / { isbin=1 }
    { section = section $0 "\n" }
    END { flush() }
  ' "$1"
}"""

TEST_CMD = r"""yarn install --ignore-scripts >/dev/null 2>&1

JEST=node_modules/.bin/jest
if [ -f jest/config/config.js ]; then
    "$JEST" --config jest/config/config.js --ci --verbose 2>&1
else
    "$JEST" --config packages/core/jest.config.js --rootDir packages/core --ci --verbose 2>&1
    if [ -d packages/clippy ]; then
        "$JEST" --rootDir packages/clippy --ci --verbose 2>&1
    fi
fi"""


class React95ImageBase(Image):
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
        return "node:14-bullseye"

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

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class React95ImageDefault(Image):
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
        return React95ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --ignore-scripts || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{test_cmd}
""".format(repo_dir=REPO_DIR, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
if ! git apply --whitespace=nowarn /home/test.filtered.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, filter=_FILTER_BINARY, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd {repo_dir}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
filter_binary /home/fix.patch > /home/fix.filtered.patch
if ! git apply --whitespace=nowarn /home/test.filtered.patch /home/fix.filtered.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi

{test_cmd}
""".format(repo_dir=REPO_DIR, filter=_FILTER_BINARY, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        prepare_inputs = {"prepare.sh", "check_git_changes.sh"}
        pre, post = "", ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name in prepare_inputs:
                pre += line
            else:
                post += line

        return f"""FROM {name}:{tag}

{self.global_env}

{pre}
RUN bash /home/prepare.sh

{post}
{self.clear_env}

"""


@Instance.register("React95", "React95")
class REACT95(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return React95ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        suite_re = re.compile(r"^(PASS|FAIL)\s+(?:\S+\s+)?(\S+\.[jt]sx?)\b")
        case_re = re.compile(
            r"^\s+(?P<mark>[✓✔✕×✗○])\s+(?:skipped\s+)?(?P<name>.+?)"
            r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )

        current = ""
        for line in log.split("\n"):
            sm = suite_re.match(line)
            if sm:
                current = sm.group(2)
                if sm.group(1) == "FAIL":
                    failed_tests.add(current)
                continue

            cm = case_re.match(line)
            if not cm:
                continue
            name = cm.group("name").strip()
            tid = f"{current} > {name}" if current else name
            mark = cm.group("mark")
            if mark in "✓✔":
                passed_tests.add(tid)
            elif mark in "✕×✗":
                failed_tests.add(tid)
            else:
                skipped_tests.add(tid)

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
