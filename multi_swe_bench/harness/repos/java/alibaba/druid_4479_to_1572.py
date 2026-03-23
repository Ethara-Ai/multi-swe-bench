from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class DruidImageBase4479To1572(Image):
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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "base-mvn"

    def workdir(self) -> str:
        return "base-mvn"

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

        global_env_section = f"\n{self.global_env}\n" if self.global_env else "\n"
        return f"""FROM {image_name}
{global_env_section}RUN ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV LC_ALL=C.UTF-8
ENV TZ=UTC

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends gnupg ca-certificates git curl maven \
    && rm -rf /var/lib/apt/lists/*
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends zulu8-jdk \
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DruidImageDefault4479To1572(Image):
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
        return DruidImageBase4479To1572(self.pr, self._config)

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
git config core.autocrlf input
git config core.filemode false
echo ".gitattributes" >> .git/info/exclude
echo "*.zip binary" >> .gitattributes
echo "*.png binary" >> .gitattributes
echo "*.jpg binary" >> .gitattributes
git add .
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

mvn -V --no-transfer-progress clean package -DskipTests || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
mvn -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
mvn -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false

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


@Instance.register("alibaba", "druid_4479_to_1572")
class DRUID_4479_TO_1572(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DruidImageDefault4479To1572(self.pr, self._config)

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
        # Strip ANSI escape sequences
        ansi_escape_pattern = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log = ansi_escape_pattern.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Surefire 2.x format: class name on "Running <class>" line,
        # results on next "Tests run: ..." line (ends with "sec", no class name)
        running_pattern = re.compile(r"Running\s+(\S+)")
        result_pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)"
        )

        current_class = None
        for line in test_log.splitlines():
            running_match = running_pattern.search(line)
            if running_match:
                current_class = running_match.group(1)
                continue

            result_match = result_pattern.search(line)
            if result_match and current_class:
                tests_run = int(result_match.group(1))
                failures = int(result_match.group(2))
                errors = int(result_match.group(3))
                skipped = int(result_match.group(4))

                if failures > 0 or errors > 0:
                    failed_tests.add(current_class)
                elif skipped == tests_run:
                    skipped_tests.add(current_class)
                elif (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(current_class)

                current_class = None

        # Deduplicate — worst result wins
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
