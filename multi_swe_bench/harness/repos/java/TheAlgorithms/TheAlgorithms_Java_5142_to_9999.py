"""TheAlgorithms/Java config for PRs 5142-9999 (JDK 21, Maven era)."""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheAlgorithmsJava5142ImageDefault(Image):
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
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # JDK 21 + Maven on top of the default toolchain (git + ca-certificates
        # are already in the base package set in Image.dockerfile()).
        return ["openjdk-21-jdk", "maven"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Sets the UTF-8 locale Maven expects, stages the helper scripts +
        # patches into /home/, and runs prepare.sh (which writes the Maven mirror
        # config under ~/.m2, outside the git tree, so the hardening pass leaves
        # it untouched).
        return (
            "ENV LC_ALL=C.UTF-8\n"
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
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# resets the tree and writes the Maven mirror config (~/.m2 lives outside the
# git tree, so the hardening pass leaves it untouched).
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

if [ ! -f ~/.m2/settings.xml ]; then
    mkdir -p ~/.m2 && cat <<EOF > ~/.m2/settings.xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">

    <mirrors>
        <mirror>
            <id>aliyunmaven</id>
            <mirrorOf>central</mirrorOf>
            <name>Aliyun Maven Mirror</name>
            <url>https://maven.aliyun.com/repository/public</url>
        </mirror>
    </mirrors>

</settings>
EOF
else
  grep -q "<mirror>" ~/.m2/settings.xml || sed -i '/<\\/settings>/i \\
  <mirrors> \\
      <mirror> \\
          <id>aliyunmaven</id> \\
          <mirrorOf>central</mirrorOf> \\
          <name>Aliyun Maven Mirror</name> \\
          <url>https://maven.aliyun.com/repository/public</url> \\
      </mirror> \\
  </mirrors>' ~/.m2/settings.xml
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
mvn clean test -Dstyle.color=never
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
mvn clean test -Dstyle.color=never

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn clean test -Dstyle.color=never

""".format(pr=self.pr),
            ),
        ]


@Instance.register("TheAlgorithms", "TheAlgorithms_Java_5142_to_9999")
class TheAlgorithmsJava5142To9999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheAlgorithmsJava5142ImageDefault(self.pr, self._config)

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
        re_running = re.compile(r"\[INFO\] Running (.+)")
        re_error = re.compile(r"\[ERROR\] Tests run")

        passed_tests = set()
        failed_tests = set()

        current_test = None
        passed_count = 0
        failed_count = 0

        for line in test_log.split("\n"):
            if current_test is None:
                running_match = re_running.search(line)
                if running_match:
                    current_test = running_match.group(1)
                continue

            if re_error.search(line):
                failed_tests.add(current_test)
                failed_count += 1
            else:
                passed_tests.add(current_test)
                passed_count += 1

            current_test = None

        return TestResult(
            passed_count=passed_count,
            failed_count=failed_count,
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
