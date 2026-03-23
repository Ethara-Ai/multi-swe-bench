import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SpringFrameworkImageBase(Image):
    """Base Docker image for Spring Framework PRs using JDK 17 (default)."""

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt update && apt install -y gnupg ca-certificates git curl
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt update && apt install -y zulu17-jdk zulu21-jdk zulu24-jdk zulu25-jdk

ENV JAVA_HOME=/usr/lib/jvm/zulu17 JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"

{code}

{copy_commands}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK11(Image):
    """Base Docker image for Spring Framework PRs using JDK 11 (for master PRs >= 22000, 5.2.x)."""

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
        return "base-jdk-11"

    def workdir(self) -> str:
        return "base-jdk-11"

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt update && apt install -y gnupg ca-certificates git curl
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt update && apt install -y zulu11-jdk

ENV JAVA_HOME=/usr/lib/jvm/zulu11 JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"

{code}

{copy_commands}

{self.clear_env}

"""


class SpringFrameworkImageBaseJDK8(Image):
    """Base Docker image for Spring Framework PRs using JDK 8 (for old master PRs < 2000, 3.2.x/4.2.x)."""

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
        return "base-jdk-8"

    def workdir(self) -> str:
        return "base-jdk-8"

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt update && apt install -y gnupg ca-certificates git curl
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list
RUN apt update && apt install -y zulu8-jdk

ENV JAVA_HOME=/usr/lib/jvm/zulu8 JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"

{code}

{copy_commands}

{self.clear_env}

"""


class SpringFrameworkImageDefault(Image):
    """Per-PR Docker image for Spring Framework with branch+PR-based JDK selection.

    JDK Selection Logic:
    - JDK 8:  master PRs < 2000 (2017 era, Gradle 3.x-4.x), 3.2.x, 4.2.x branches
    - JDK 11: master PRs >= 22000 (2019-2021, Gradle 5.x-6.x), 5.2.x branch
    - JDK 17: main branch (SF 6.0+, Gradle 7.3+), 6.2.x branch (default)
    """

    JDK_8_BRANCHES = {"3.2.x", "4.2.x"}
    JDK_11_BRANCHES = {"5.2.x"}

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _select_jdk(self) -> str:
        ref = self.pr.base.ref
        if ref in self.JDK_8_BRANCHES:
            return "8"
        if ref in self.JDK_11_BRANCHES:
            return "11"
        if ref == "master" and self.pr.number < 2000:
            return "8"
        if ref == "master" and self.pr.number < 25000:
            return "11"
        if ref == "master":
            # Late master PRs (25000+) overlap with early main branch and need JDK 17
            return "17"
        return "17"

    def dependency(self) -> Image | None:
        jdk = self._select_jdk()
        if jdk == "8":
            return SpringFrameworkImageBaseJDK8(self.pr, self._config)
        if jdk == "11":
            return SpringFrameworkImageBaseJDK11(self.pr, self._config)
        return SpringFrameworkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        jdk = self._select_jdk()
        if jdk == "17":
            gradlew_build = "./gradlew build -x test -x java21Test -x java22Test -x java25Test -x checkstyleMain -x checkstyleTest -x compileTestJava -x compileTestKotlin -x compileTestGroovy || true"
        else:
            gradlew_build = "./gradlew build -x test || true"

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

# Strip dead CI/build-scan plugins that block Gradle settings evaluation
# These plugins (gradle-enterprise-conventions, ge.conventions) only add CI metadata
# (Git info, build scans, OS tags) with zero effect on compilation or test execution.
# They fail because repo.spring.io returns 401 or the plugins were removed from registries.
sed -i "/io.spring.gradle-enterprise-conventions/d" settings.gradle 2>/dev/null || true
sed -i "/io.spring.gradle-enterprise-conventions/d" build.gradle 2>/dev/null || true
sed -i "/com.gradle.build-scan/d" build.gradle 2>/dev/null || true
sed -i '/io.spring.ge.conventions/d' settings.gradle 2>/dev/null || true
sed -i '/com.gradle.enterprise/d' settings.gradle 2>/dev/null || true

# Also strip the gradleEnterprise/develocity configuration blocks that reference the removed plugins.
# These blocks are always the last block in settings.gradle (settings.gradle.projectsLoaded block).
# Without this, Gradle fails with "Could not find method gradleEnterprise()" after plugin line removal.
sed -i '/settings.gradle.projectsLoaded/,$d' settings.gradle 2>/dev/null || true
sed -i '/^gradleEnterprise[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true
sed -i '/^develocity[[:space:]]*{{/,$d' settings.gradle 2>/dev/null || true

# Upgrade old Gradle wrapper for PRs that can't handle newer JDK class files
# Conservative mapping for JDK 8 PRs (keep Gradle era-appropriate),
# aggressive upgrade to 7.6.4 for JDK 11/17 PRs (proven to work by PR-24105).
if grep -q "gradle-4" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    if [ "$JAVA_HOME" = "/usr/lib/jvm/zulu8" ]; then
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10.3-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    else
        sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
    fi
elif grep -q -e "gradle-5" -e "gradle-6" gradle/wrapper/gradle-wrapper.properties 2>/dev/null; then
    sed -i 's|distributionUrl=.*|distributionUrl=https\\://services.gradle.org/distributions/gradle-7.6.4-bin.zip|' gradle/wrapper/gradle-wrapper.properties
fi

chmod +x gradlew
{gradlew_build}

""".format(pr=self.pr, gradlew_build=gradlew_build),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
./gradlew clean test --continue --max-workers 4

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch; then
    echo "WARNING: git apply failed for test.patch, continuing with tests anyway"
fi
./gradlew clean test --continue --max-workers 4

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch /home/fix.patch; then
    echo "WARNING: git apply failed for test.patch/fix.patch, continuing with tests anyway"
fi
./gradlew clean test --continue --max-workers 4

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
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc && \\
                        /bin/bash -c "source ~/.bashrc"
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
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


@Instance.register("spring-projects", "spring-framework")
class SpringFramework(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringFrameworkImageDefault(self.pr, self._config)

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

        # Gradle task-level patterns: these are the primary signal because
        # Spring's Gradle config does NOT output individual "ClassName > method PASSED"
        # lines by default — only FAILED individual tests appear in output.
        # We keep task-level patterns for passed/skipped, but for FAILED we
        # EXCLUDE compile tasks (compileJava, compileTestJava, compileTestKotlin, etc.)
        # because compile failures are NOT test failures — they cause false
        # PASS→FAIL regressions that block valid resolutions.
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

        # Compile task names to exclude from FAILED counts — these are build
        # tasks, not test tasks. When compileJava succeeds in run/test but
        # fails in fix, it creates a false PASS→FAIL regression.
        compile_task_re = re.compile(r"^.*:(compile\w*|processResources|processTestResources|classes|testClasses|jar|testFixturesClasses|testFixturesJar)")

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    task_name = m.group(1)
                    # Skip compile/build tasks — only count test task failures
                    if compile_task_re.match(task_name):
                        continue
                    failed_tests.add(task_name)
                    if task_name in passed_tests:
                        passed_tests.remove(task_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        # Post-loop deduplication (worst result wins: failed > skipped > passed)
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
