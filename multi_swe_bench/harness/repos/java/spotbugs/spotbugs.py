import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SpotbugsImageBase(Image):
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
        return "ubuntu:22.04"

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

WORKDIR /home/
RUN apt-get update && apt-get install -y git

{code}

{self.clear_env}

"""


class SpotbugsImageDefault(Image):
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
        return SpotbugsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _jdk_version(self) -> int:
        if self.pr.number <= 1734:
            return 11
        elif self.pr.number <= 2679:
            return 17
        return 21

    def files(self) -> list[File]:
        jdk = self._jdk_version()
        if self.pr.number <= 2679:
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

apt-get update && apt-get install -y openjdk-{jdk}-jdk
export JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk-$(dpkg --print-architecture)
export PATH=$JAVA_HOME/bin:$PATH

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
mkdir -p ~/.gradle && cat <<EOF > ~/.gradle/init.gradle
allprojects {{
    buildscript {{
        repositories {{
            maven {{ url 'https://maven.aliyun.com/repository/public/' }}
            maven {{ url 'https://maven.aliyun.com/repository/google/' }}
        }}
    }}

    repositories {{
        maven {{ url 'https://maven.aliyun.com/repository/public/' }}
        maven {{ url 'https://maven.aliyun.com/repository/google/' }}
    }}

    tasks.withType(Test).configureEach {{
        testLogging {{
            events "passed", "failed", "skipped"
            exceptionFormat "full"
            showStandardStreams false
        }}
    }}
}}
EOF
./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 8 --continue || true
""".format(pr=self.pr, jdk=jdk),
                ),
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 8 --continue
""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 8 --continue

""".format(pr=self.pr),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 8 --continue

""".format(pr=self.pr),
                ),
            ]
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

apt-get update && apt-get install -y openjdk-{jdk}-jdk
export JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk-$(dpkg --print-architecture)
export PATH=$JAVA_HOME/bin:$PATH

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
./gradlew clean test --continue || true
""".format(pr=self.pr, jdk=jdk),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --continue
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
./gradlew clean test --continue

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
./gradlew clean test --continue

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


@Instance.register("spotbugs", "spotbugs")
class Spotbugs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SpotbugsImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
