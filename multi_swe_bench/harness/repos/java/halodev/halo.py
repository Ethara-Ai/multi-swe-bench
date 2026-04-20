from __future__ import annotations

import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ──────────────────────────────────────────────────────────────
# JDK selection verified against all 188 base commits (2025-04-16):
#
#   JDK 8  (15 PRs): master #174-#1039, dev #769
#   JDK 11 (13 PRs): master #1217-#2143, master #5562
#   JDK 17 (157 PRs): next #2138-#2488, main #2522-#7407
#   JDK 21 (3 PRs): main #7665, #7990, #8304
#
# Gradle wrapper auto-downloads Node.js/pnpm via the
# com.github.node-gradle.node plugin for PRs with a ui/ subproject
# (main #5320+), so no system Node.js installation is needed.
# ──────────────────────────────────────────────────────────────


def _select_jdk(pr: PullRequest) -> str:
    ref = pr.base.ref
    if ref == "dev":
        return "8"
    if ref == "master":
        if pr.number < 1217:
            return "8"
        return "11"
    if ref == "next":
        return "17"
    if pr.number >= 7665:
        return "21"
    return "17"


def _zulu_package(jdk: str) -> str:
    return f"zulu{jdk}-jdk"


def _java_home(jdk: str) -> str:
    return f"/usr/lib/jvm/zulu{jdk}"


class _HaloImageBaseFactory:
    @staticmethod
    def create(pr: PullRequest, config: Config) -> Image:
        jdk = _select_jdk(pr)
        return _HaloImageBase(pr, config, jdk)


class _HaloImageBase(Image):

    def __init__(self, pr: PullRequest, config: Config, jdk: str):
        self._pr = pr
        self._config = config
        self._jdk = jdk

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        if self._jdk == "17":
            return "base"
        return f"base-jdk{self._jdk}"

    def workdir(self) -> str:
        return self.image_tag()

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

        zulu = _zulu_package(self._jdk)
        java_home = _java_home(self._jdk)

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl unzip git \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://repos.azul.com/azul-repo.key -o /usr/share/keyrings/azul.asc \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.asc] https://repos.azul.com/zulu/deb stable main" > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends {zulu} \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME={java_home}
ENV PATH=$JAVA_HOME/bin:$PATH

{code}

{self.clear_env}

"""


class HaloImageDefault(Image):

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
        return _HaloImageBaseFactory.create(self.pr, self._config)

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# jcenter() shut down Feb 2024 — remove it so Gradle does not hang
sed -i '/jcenter()/d' build.gradle 2>/dev/null || true
sed -i '/jcenter()/d' settings.gradle 2>/dev/null || true

# Replace China-only aliyun mirror with mavenCentral for portability
sed -i '/maven.aliyun.com/d' build.gradle 2>/dev/null || true

chmod +x gradlew
./gradlew build -x test -x check || true
""".format(pr=self.pr),
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


@Instance.register("halo-dev", "halo")
class Halo(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HaloImageDefault(self.pr, self._config)

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

        compile_task_re = re.compile(
            r"^.*:(compile\w*|processResources|processTestResources|classes|testClasses|jar)"
        )

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    task_name = m.group(1)
                    if compile_task_re.match(task_name):
                        continue
                    failed_tests.add(task_name)
                    if task_name in passed_tests:
                        passed_tests.remove(task_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

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
