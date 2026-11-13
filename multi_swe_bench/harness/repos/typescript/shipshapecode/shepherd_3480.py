from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "shepherd"
PNPM_VERSION = "11.21.0"

TESTS_PY = r"""import json
import os
import re
import sys

REPO = "/home/[[REPO_DIR]]"


def vitest_json(json_file):
    try:
        data = json.load(open(json_file, encoding="utf-8"))
    except (OSError, ValueError):
        return
    status_map = {"passed": "PASSED", "failed": "FAILED"}
    results = {}
    rank = {"SKIPPED": 0, "PASSED": 1, "FAILED": 2}
    for suite in data.get("testResults", []):
        rel = os.path.relpath(suite.get("name", ""), REPO)
        for a in suite.get("assertionResults", []):
            parts = [rel] + [re.sub(r"\s+", " ", t).strip() for t in a.get("ancestorTitles", [])]
            parts.append(re.sub(r"\s+", " ", a.get("title", "")).strip())
            tid = " > ".join(parts)
            status = status_map.get(a.get("status"), "SKIPPED")
            if tid not in results or rank[status] > rank[results[tid]]:
                results[tid] = status
    for tid in sorted(results):
        print("VITEST " + results[tid] + " " + tid)


if __name__ == "__main__":
    {"vitest_json": vitest_json}[sys.argv[1]](sys.argv[2])
""".replace("[[REPO_DIR]]", REPO_DIR)

TESTS_SH = r"""#!/bin/bash
REPO=/home/[[REPO_DIR]]
HELPER=/home/shepherd_tests.py
export CI=true
export CYPRESS_INSTALL_BINARY=0

setup() {
    local i
    cd $REPO
    for i in 1 2 3; do
        pnpm install --frozen-lockfile --filter shepherd.js && break
        [ $i -eq 3 ] && return 1
        sleep 30
    done
    cd $REPO/shepherd.js
    npx vitest run --reporter=dot > /tmp/shepherd_warm.log 2>&1
    tail -n 5 /tmp/shepherd_warm.log
    return 0
}

run_tests() {
    cd $REPO/shepherd.js
    rm -f /tmp/shepherd_vitest.json
    npx vitest run --reporter=default --reporter=json --outputFile.json=/tmp/shepherd_vitest.json > /tmp/shepherd_vitest.log 2>&1
    tail -n 120 /tmp/shepherd_vitest.log
    python3 $HELPER vitest_json /tmp/shepherd_vitest.json
}

case "$1" in
    setup) setup ;;
    run) run_tests ;;
esac
""".replace("[[REPO_DIR]]", REPO_DIR)


class Shepherd_3480_ImageBase(Image):
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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base-node24"

    def workdir(self) -> str:
        return "base-node24"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates && rm -rf /var/lib/apt/lists/*

RUN npm install -g pnpm@{PNPM_VERSION} && pnpm --version

WORKDIR /home/

{code}

{self.clear_env}

"""


class Shepherd_3480_ImageDefault(Image):
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
        return Shepherd_3480_ImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "shepherd_tests.py",
                TESTS_PY,
            ),
            File(
                ".",
                "shepherd_tests.sh",
                TESTS_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

bash /home/shepherd_tests.sh setup

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
bash /home/shepherd_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/shepherd_tests.sh run

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/shepherd_tests.sh run

""".format(repo_dir=REPO_DIR),
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


@Instance.register("shipshapecode", "shepherd_3480")
class Shepherd_3480(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Shepherd_3480_ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        re_result = re.compile(r"^VITEST\s+(PASSED|FAILED|SKIPPED)\s+(.+?)\s*$")

        status_map = {
            "PASSED": passed_tests,
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).rstrip()
            m = re_result.match(line)
            if m:
                status_map[m.group(1)].add(m.group(2))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("shipshapecode", "shepherd")(Shepherd_3480)
