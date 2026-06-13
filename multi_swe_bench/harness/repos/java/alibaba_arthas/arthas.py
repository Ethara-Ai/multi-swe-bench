import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# alibaba/arthas — a Java JVM diagnostic tool, built with Maven (multi-module).
#
# Discovery (verified in Docker, maven:3.9.6-eclipse-temurin-8):
#  - The 35-PR range #48..#3140 spans arthas 3.0.4 (2018, compiler target 1.6,
#    7 modules) to arthas 4.1.9 (2026, target 1.8, ~25 modules). JDK 8 builds
#    the whole range — it is the last JDK that still accepts `-source 1.6`.
#  - One config, no era split: Maven supplies surefire 3.2.2 regardless of the
#    pom, so the test-output format is uniform across all eras:
#       Tests run: N, Failures: F, Errors: E, Skipped: S, Time elapsed: T s \
#         [<<< FAILURE!] -- in <FullyQualifiedTestClass>
#  - `arthas-vmtool` compiles C++ via native-maven-plugin → base image needs
#    g++ (build-essential). `-fae` keeps the reactor going past any module
#    that still fails to build, so unrelated modules' tests are still measured.
#
# Known excluded outlier — PR #2642 (1 of 35): its test is a Spring Boot 3
# maven-invoker integration test under `arthas-spring-boot-starter/src/it/`.
# It needs JDK 17 (Spring Boot 3 mandates Java 17+) and runs only under
# `mvn verify` via the invoker plugin — neither compatible with the JDK 8
# base that the oldest PRs require (`-source 1.6`, rejected by JDK 11/17).
# Covering it would need a dedicated JDK-17 era config; deliberately not done.
# PR #2642 is expected not to resolve; all other 34 PRs are covered.


class ArthasImageBase(Image):
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
        # Maven 3.9.6 + JDK 8 (multi-arch: amd64 + arm64). JDK 8 is required —
        # later JDKs drop `-source 1.6` support needed by the oldest PRs.
        return "maven:3.9.6-eclipse-temurin-8"

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

        return f"""FROM {image_name}

{self.global_env}

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"
ENV MAVEN_OPTS="-Xmx2g"
# MAVEN_ARGS (read by Maven 3.9+) applies to every mvn call: disable HTTP
# connection pooling / keep-alive and retry failed downloads — mitigates the
# "Premature end of Content-Length delimited message body" transfer errors.
ENV MAVEN_ARGS="-Dmaven.wagon.http.pool=false -Dhttp.keepAlive=false -Dmaven.wagon.httpconnectionManager.ttlSeconds=120 -Dmaven.wagon.http.retryHandler.count=5"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

# git for checkout, build-essential (g++) for the arthas-vmtool native build.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ArthasImageDefault(Image):
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
        return ArthasImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
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

# Maven Central is used directly (no mirror) — measured faster and more
# reliable than the aliyun mirror from this build environment.

# Warm the ~/.m2 dependency cache: compile main + test sources at the base
# commit (downloads every dependency the test phase needs). `clean test`
# later only recompiles; no re-download.
mvn -V -B --no-transfer-progress -fae clean test-compile || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
EXCLUDES="--exclude=*.jar --exclude=*.png --exclude=*.PNG --exclude=*.gif \\
--exclude=*.ico --exclude=*.ttf --exclude=*.woff --exclude=*.woff2 \\
--exclude=*.jpg --exclude=*.jpeg --exclude=*.zip --exclude=*.so --exclude=*.dylib"
git apply --whitespace=nowarn $EXCLUDES /home/test.patch \\
  || git apply --whitespace=nowarn --reject $EXCLUDES /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
EXCLUDES="--exclude=*.jar --exclude=*.png --exclude=*.PNG --exclude=*.gif \\
--exclude=*.ico --exclude=*.ttf --exclude=*.woff --exclude=*.woff2 \\
--exclude=*.jpg --exclude=*.jpeg --exclude=*.zip --exclude=*.so --exclude=*.dylib"
git apply --whitespace=nowarn $EXCLUDES /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject $EXCLUDES /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
mvn -V -B --no-transfer-progress -fae clean test \\
    -Dsurefire.useFile=false -DfailIfNoTests=false -Dmaven.test.failure.ignore=true

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


@Instance.register("alibaba", "arthas")
class Arthas(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ArthasImageDefault(self.pr, self._config)

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
        # Strip ANSI escape sequences first.
        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Surefire per-class summary line (test granularity = test class):
        #   Tests run: 1, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.8 s -- in com.Foo
        #   Tests run: 19, Failures: 2, Errors: 0, Skipped: 0, Time elapsed: 1.2 s <<< FAILURE! -- in com.Bar
        # The non-greedy `.+?` absorbs the optional `s <<< FAILURE! --` segment.
        # The aggregate "Results:" line has no "Time elapsed" so it never matches.
        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), "
            r"Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in clean.splitlines():
            m = pattern.search(line)
            if not m:
                continue
            tests_run = int(m.group(1))
            failures = int(m.group(2))
            errors = int(m.group(3))
            skipped = int(m.group(4))
            name = m.group(5).strip()

            if failures > 0 or errors > 0:
                failed_tests.add(name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(name)
            elif tests_run > 0:
                passed_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
