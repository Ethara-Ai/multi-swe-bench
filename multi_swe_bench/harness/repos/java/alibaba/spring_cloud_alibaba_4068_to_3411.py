from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SpringCloudAlibabaImageBaseJava17(Image):
    """Base image for spring-cloud-alibaba PRs #3411–#4068 (Java 17 era).

    Covers Spring Cloud Alibaba 2022.0.0.0 through 2025.1.0.0.
    Spring Boot 3.x, uses Azul Zulu JDK 17 on Ubuntu 22.04.
    Build tool: Maven Wrapper (./mvnw) with Maven 3.9.0.
    """

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
        return "base-java17"

    def workdir(self) -> str:
        return "base-java17"

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

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y gnupg ca-certificates git curl && rm -rf /var/lib/apt/lists/*
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y zulu17-jdk && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SpringCloudAlibabaImageDefaultJava17(Image):
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
        return SpringCloudAlibabaImageBaseJava17(self.pr, self._config)

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

# Configure Maven mirror for faster downloads
if [ ! -f ~/.m2/settings.xml ]; then
    mkdir -p ~/.m2 && cat <<EOFXML > ~/.m2/settings.xml
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
EOFXML
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

chmod +x ./mvnw 2>/dev/null || true
./mvnw -V --no-transfer-progress clean package -DskipTests -Dmaven.javadoc.skip=true || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./mvnw -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.gif' /home/test.patch
./mvnw -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.gif' /home/test.patch /home/fix.patch
./mvnw -V --no-transfer-progress clean test -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false
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
                m = re.match(r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line)
                if m:
                    proxy_host = m.group(2)
                    proxy_port = m.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p ~/.m2 && \\
                        if [ ! -f ~/.m2/settings.xml ]; then \\
                            cat > ~/.m2/settings.xml <<XML
<settings xmlns=\"http://maven.apache.org/SETTINGS/1.0.0\"
          xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"
          xsi:schemaLocation=\"http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd\">
  <proxies>
    <proxy>
      <id>proxy</id>
      <active>true</active>
      <protocol>http</protocol>
      <host>{proxy_host}</host>
      <port>{proxy_port}</port>
    </proxy>
  </proxies>
</settings>
XML
                        fi
                """.format(proxy_host=proxy_host, proxy_port=proxy_port)
                )
                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\/proxies>/d' ~/.m2/settings.xml
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


@Instance.register("alibaba", "spring_cloud_alibaba_4068_to_3411")
class SPRING_CLOUD_ALIBABA_4068_TO_3411(Instance):
    """spring-cloud-alibaba PRs #3411–#4068: Java 17 era.

    Covers 2022.0.0.0 through 2025.1.0.0.
    Build: Maven Wrapper + Azul Zulu JDK 17.
    Spring Boot 3.x, JUnit 5.
    Test output: Maven Surefire 2.22.2+ format.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringCloudAlibabaImageDefaultJava17(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Maven Surefire output pattern:
        # Tests run: N, Failures: N, Errors: N, Skipped: N, Time elapsed: N.NNN s - in <class>
        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in clean_log.splitlines():
            match = pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5).strip()

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

        # Deduplicate: worst result wins
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
