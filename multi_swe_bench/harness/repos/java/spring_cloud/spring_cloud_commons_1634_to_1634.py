from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SpringCloudCommonsImageBaseJava17(Image):
    """Shared base image for the spring-cloud/spring-cloud-commons 4.3.x era.

    PR #1634 sits on the `4.3.x` maintenance branch, whose reactor targets Java
    17 (`spring-cloud-build` 4.3.3-SNAPSHOT parent) and ships a Maven Wrapper
    pinned to Maven 3.6.3.  The repository clone lives in this image so the
    pipeline's `_standardize_repo_fetch` rewrite (triggered because
    `dependency()` returns a plain string) can pin and harden it.

    NOTE: that rewrite pins the clone to `${BASE_COMMIT}` and then hardens it
    destructively (every ref deleted, `git gc --prune=now`), so this shared base
    is reusable only while the range holds exactly one PR.  Adding a second
    `spring-cloud-commons` row -- which the plain-key alias at the bottom of this
    file would route here -- must first move the clone out of this base and into
    the per-PR image, otherwise `prepare.sh` would `git checkout` a sha that no
    longer exists in the pruned history.
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
        return "eclipse-temurin:17-jdk"

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

RUN apt-get update && apt-get install -y ca-certificates git curl unzip && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SpringCloudCommonsImageDefaultJava17(Image):
    """Per-PR image for the spring-cloud/spring-cloud-commons 4.3.x era."""

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
        return SpringCloudCommonsImageBaseJava17(self.pr, self._config)

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

""".format(pr=self.pr),
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

chmod +x ./mvnw

./mvnw -V -B --no-transfer-progress -fae clean test -Dtest="*Tests,*Test,!AbstractAutoServiceRegistrationTests,!AbstractAutoServiceRegistrationRegistrationLifecycleTests" -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dspring-javaformat.skip=true -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.test.redirectTestOutputToFile=true || true

git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
./mvnw -o -B -fae clean test -Dtest="*Tests,*Test,!AbstractAutoServiceRegistrationTests,!AbstractAutoServiceRegistrationRegistrationLifecycleTests" -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dspring-javaformat.skip=true -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.test.redirectTestOutputToFile=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.gif' /home/test.patch

./mvnw -o -B -fae clean test -Dtest="*Tests,*Test,!AbstractAutoServiceRegistrationTests,!AbstractAutoServiceRegistrationRegistrationLifecycleTests" -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dspring-javaformat.skip=true -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.test.redirectTestOutputToFile=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.gif' /home/test.patch /home/fix.patch

./mvnw -o -B -fae clean test -Dtest="*Tests,*Test,!AbstractAutoServiceRegistrationTests,!AbstractAutoServiceRegistrationRegistrationLifecycleTests" -Dsurefire.failIfNoSpecifiedTests=false -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true -Dspring-javaformat.skip=true -Dcheckstyle.skip=true -Dmaven.javadoc.skip=true -Dmaven.test.redirectTestOutputToFile=true

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
            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line.strip()
                )
                if match:
                    host = match.group(2)
                    port = match.group(3)
                    proxy_setup = textwrap.dedent(
                        f"""
                        RUN mkdir -p ~/.m2 && printf '%s\\n' \\
                            '<settings>' \\
                            '  <proxies>' \\
                            '    <proxy>' \\
                            '      <id>global-proxy</id>' \\
                            '      <active>true</active>' \\
                            '      <protocol>http</protocol>' \\
                            '      <host>{host}</host>' \\
                            '      <port>{port}</port>' \\
                            '    </proxy>' \\
                            '  </proxies>' \\
                            '</settings>' > ~/.m2/settings.xml
                        """
                    ).strip()
                    proxy_cleanup = "RUN sed -i '/<active>true<\\/active>/s//<active>false<\\/active>/' ~/.m2/settings.xml"
                    break

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}
{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("spring-cloud", "spring_cloud_commons_1634_to_1634")
class SPRING_CLOUD_COMMONS_1634_TO_1634(Instance):
    """spring-cloud/spring-cloud-commons, PR range 1634..1634 (4.3.x, Java 17, Maven Wrapper 3.6.3).

    The reactor binds `spring-javaformat-maven-plugin:apply`, which rewrites
    tracked sources, and `maven-checkstyle-plugin:check`; both are skipped so no
    build step can dirty the work tree and break `git apply` (R21).  The `spring`
    profile carrying the repo.spring.io snapshot repositories is auto-activated
    by the repository's own `.mvn/maven.config`, so the SNAPSHOT parent resolves
    without touching any build file (R22).

    The parent POM (`org.springframework.cloud:spring-cloud-build`) configures
    surefire with `<exclude>**/Abstract*.java</exclude>`.  The gold test added by
    this PR's test patch is `AbstractEnvironmentDecryptTests` -- named after the
    production class `AbstractEnvironmentDecrypt` it covers -- so the project's
    own exclude silently filters out all 13 of its cases even though the class is
    concrete and compiles.  Without an override the three stages are identical and
    the instance grades INVALID.  `-Dtest=...` overrides the POM's includes and
    excludes from the command line, which keeps the build files untouched (R22)
    and is applied byte-identically in all three stages (R3).

    Re-admitting `Abstract*` re-admits three unrelated concrete base-class tests in
    `spring-cloud-commons`.  Each was executed on both the baseline tree and the
    test+fix tree to determine its true behaviour:

      * `AbstractAutoServiceRegistrationTests`                     FAIL -> FAIL
      * `AbstractAutoServiceRegistrationRegistrationLifecycleTests` FAIL -> FAIL
      * `AbstractAutoServiceRegistrationMgmtDisabledTests`          PASS -> PASS

    Only the first two are negated: they fail identically before and after the fix
    (they are written to be driven by a subclass), so they can never be graded and
    would only add noise.  The third passes in every stage and is deliberately left
    in the suite as a p2p guard.  The remaining two `Abstract*` test files in the
    reactor are genuinely `abstract` and are skipped by surefire on their own.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringCloudCommonsImageDefaultJava17(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.,]+ .+? in (.+)"
        )

        for line in clean_log.splitlines():
            match = pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5).strip()

                if failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif tests_run > 0 and skipped == tests_run:
                    skipped_tests.add(test_name)
                elif tests_run > 0:
                    passed_tests.add(test_name)

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


# The raw dataset row for PR #1634 carries neither `number_interval` nor `tag`,
# so `Instance.create` resolves the plain key `spring-cloud/spring-cloud-commons`.
# Registering that key as an alias of the range class is the sanctioned remedy
# (HOW_TO_CREATE_REPO_CONFIG.md R26 / 17.4) and is correct here because this
# class's toolchain fits every PR in the interval.
Instance.register("spring-cloud", "spring-cloud-commons")(
    SPRING_CLOUD_COMMONS_1634_TO_1634
)
