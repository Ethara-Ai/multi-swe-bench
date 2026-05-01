import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SkywalkingImageBase(Image):
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

        global_env = self.global_env
        global_env_block = f"\n{global_env}\n" if global_env.strip() else ""

        return f"""FROM {image_name}
{global_env_block}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        curl \\
        make && \\
    rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SkywalkingImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return SkywalkingImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @classmethod
    def _jdk_package(cls, pr_number: int) -> str:
        if pr_number >= 10298:
            return "openjdk-11-jdk"
        return "openjdk-8-jdk"

    @classmethod
    def _mvn_cmd(cls, pr_number: int) -> str:
        if pr_number >= 1619:
            return "./mvnw"
        return "mvn"

    @classmethod
    def _submodule_cmd(cls, pr_number: int) -> str:
        if pr_number >= 1210:
            return "git submodule update --init --recursive"
        return ""

    def files(self) -> list[File]:
        jdk_pkg = self._jdk_package(self.pr.number)
        mvn_cmd = self._mvn_cmd(self.pr.number)
        submodule_cmd = self._submodule_cmd(self.pr.number)

        submodule_step = ""
        if submodule_cmd:
            submodule_step = submodule_cmd

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

apt-get update && apt-get install -y --no-install-recommends {jdk_pkg} maven && rm -rf /var/lib/apt/lists/*

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
if [ -f .gitmodules ]; then
    sed -i 's|git@github.com:|https://github.com/|g' .gitmodules
fi
{submodule_step}

{mvn_cmd} clean install -T 4 -B -q -fn -Dcheckstyle.skip -Dgpg.skip -Dmaven.javadoc.skip=true -Denforcer.skip=true -Danimal.sniffer.skip=true -Drat.skip=true -Dmdep.analyze.skip=true -DskipTests || true
""".format(
                    pr=self.pr,
                    jdk_pkg=jdk_pkg,
                    mvn_cmd=mvn_cmd,
                    submodule_step=submodule_step,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{mvn_cmd} clean install -T 4 -U -B -q -fn -Dcheckstyle.skip -Dgpg.skip -Dmaven.javadoc.skip=true -Denforcer.skip=true -Danimal.sniffer.skip=true -Drat.skip=true -Dmdep.analyze.skip=true -DskipTests
{mvn_cmd} clean test -U -B -fn -Dcheckstyle.skip -Dgpg.skip -Dsurefire.useFile=false -DfailIfNoTests=false
""".format(pr=self.pr, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch
{mvn_cmd} clean install -T 4 -U -B -q -fn -Dcheckstyle.skip -Dgpg.skip -Dmaven.javadoc.skip=true -Denforcer.skip=true -Danimal.sniffer.skip=true -Drat.skip=true -Dmdep.analyze.skip=true -DskipTests
{mvn_cmd} clean test -U -B -fn -Dcheckstyle.skip -Dgpg.skip -Dsurefire.useFile=false -DfailIfNoTests=false

""".format(pr=self.pr, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch /home/fix.patch
{mvn_cmd} clean install -T 4 -U -B -q -fn -Dcheckstyle.skip -Dgpg.skip -Dmaven.javadoc.skip=true -Denforcer.skip=true -Danimal.sniffer.skip=true -Drat.skip=true -Dmdep.analyze.skip=true -DskipTests
{mvn_cmd} clean test -U -B -fn -Dcheckstyle.skip -Dgpg.skip -Dsurefire.useFile=false -DfailIfNoTests=false

""".format(pr=self.pr, mvn_cmd=mvn_cmd),
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
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
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


@Instance.register("apache", "skywalking")
class Skywalking(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SkywalkingImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? (?:--|-) in (.+)"
        )

        for line in test_log.splitlines():
            match = pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5)

                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
