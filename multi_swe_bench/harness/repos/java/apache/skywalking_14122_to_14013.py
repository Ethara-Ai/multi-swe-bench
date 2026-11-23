import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

JDK_PACKAGE = "openjdk-11-jdk"
JAVA_HOME = "/usr/lib/jvm/java-11"

FLATC_VERSION = "1.12.0"
FLATC_SHA256 = "62f2223fb9181d1d6338451375628975775f7522185266cd5296571ac152bc45"
FLATC_PATH = "/usr/local/bin/flatc"

MAVEN_CENTRAL_MIRROR = "https://maven-central.storage-download.googleapis.com/maven2"

SETTINGS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>central-gcs-mirror</id>
      <name>Maven Central via Google Cloud Storage</name>
      <url>{MAVEN_CENTRAL_MIRROR}</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
</settings>
"""

_MAVEN_BASE_FLAGS = (
    "-B -V --no-transfer-progress "
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dlicense.skip=true "
    "-Denforcer.skip=true "
    "-Danimal.sniffer.skip=true "
    "-Dmdep.analyze.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.source.skip=true "
    "-Dgpg.skip=true "
    f"-Dfbs.compiler={FLATC_PATH}"
)

_MAVEN_RETRY_FLAGS = (
    "-Daether.connector.http.retryHandler.count=5 "
    "-Daether.connector.http.retryHandler.serviceUnavailable=429,500,502,503,504 "
    "-Daether.connector.http.retryHandler.interval=5000 "
    "-Daether.connector.http.retryHandler.intervalMax=60000 "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.class=standard "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.count=5 "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.retryInterval=5000 "
    "-Dmaven.wagon.rto=180000"
)

MAVEN_FLAGS = f"{_MAVEN_BASE_FLAGS} {_MAVEN_RETRY_FLAGS}"

INSTALL_SKIP_FLAGS = "-Dmaven.test.skip=true -DskipTests=true -DskipUTs=true"

PREPARE_SKIP_FLAGS = "-DskipTests=true -DskipUTs=true"

SUREFIRE_FLAGS = (
    "-fae "
    "-Dmaven.test.failure.ignore=true "
    "-Dsurefire.useFile=false "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

BYTE_BUDDY_VERSION_CMD = (
    "sed -n 's:.*<byte-buddy.version>\\(.*\\)</byte-buddy.version>.*:\\1:p' "
    "pom.xml | head -n 1"
)
BYTE_BUDDY_AGENT_JAR = (
    "$HOME/.m2/repository/net/bytebuddy/byte-buddy-agent/"
    "$agent_version/byte-buddy-agent-$agent_version.jar"
)

FLATTEN_VERSION_CMD = (
    "sed -n 's:.*<flatten-plugin-version>\\(.*\\)</flatten-plugin-version>.*:\\1:p' "
    "pom.xml | head -n 1"
)
FLATTEN_FALLBACK_GOAL = "flatten:flatten"
BOM_MODULE = "oap-server-bom"

REACTOR_PREFIXES = ("oap-server/", "apm-protocol/")

TEST_SOURCE_ROOT = "/src/test/java/"

COMPILE_ERROR_TEST_FILE = re.compile(
    r"^\[ERROR\]\s+(\S+" + re.escape(TEST_SOURCE_ROOT) + r"\S+\.java):\[\d+,\d+\]"
)


def _patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch or "", re.MULTILINE)


def _reactor_modules(paths: list[str]) -> list[str]:
    modules = set()
    for path in paths:
        if "/src/" not in path or not path.startswith(REACTOR_PREFIXES):
            continue
        modules.add(path.split("/src/")[0])
    return sorted(modules)


def _test_modules(pr: PullRequest) -> list[str]:
    modules = _reactor_modules(_patch_files(pr.test_patch))
    if modules:
        return modules
    return _reactor_modules(_patch_files(pr.fix_patch))


def _compile_failed_test_classes(test_log: str) -> set[str]:
    classes = set()
    for line in test_log.splitlines():
        match = COMPILE_ERROR_TEST_FILE.match(line.strip())
        if not match:
            continue
        rel = match.group(1).split(TEST_SOURCE_ROOT, 1)[1]
        classes.add(rel[: -len(".java")].replace("/", "."))
    return classes


def _existing_modules_block(modules: list[str]) -> str:
    return f"""modules=()
for module in {" ".join(modules)}; do
  if [ -f "$module/pom.xml" ]; then
    modules+=("$module")
  fi
done
"""


def _install_setup_block() -> str:
    return f"""install_modules=("${{modules[@]}}")
if [ -f "{BOM_MODULE}/pom.xml" ]; then
  install_modules+=("{BOM_MODULE}")
fi
install_selected=$(IFS=,; printf '%s' "${{install_modules[*]}}")
flatten_version=$({FLATTEN_VERSION_CMD})
if [ -n "$flatten_version" ]; then
  flatten_goal="org.codehaus.mojo:flatten-maven-plugin:$flatten_version:flatten"
else
  flatten_goal="{FLATTEN_FALLBACK_GOAL}"
fi
"""


def _test_commands(pr: PullRequest) -> str:
    modules = _test_modules(pr)
    if not modules:
        return "exit 1\n"

    install_setup = "".join(
        f"  {line}\n" for line in _install_setup_block().splitlines()
    )

    return f"""status=0

cd /home/{pr.repo}
{_existing_modules_block(modules)}if [ ${{#modules[@]}} -gt 0 ]; then
  selected=$(IFS=,; printf '%s' "${{modules[*]}}")
{install_setup}  if ./mvnw {MAVEN_FLAGS} $flatten_goal install -pl "$install_selected" -am {INSTALL_SKIP_FLAGS}; then
    if ! ./mvnw {MAVEN_FLAGS} test -pl "$selected" {SUREFIRE_FLAGS}; then
      status=1
    fi
  else
    status=1
  fi
fi

exit "$status"
"""


def _prepare_commands(pr: PullRequest) -> str:
    modules = _test_modules(pr)
    if not modules:
        return "exit 1\n"

    return f"""cd /home/{pr.repo}
{_existing_modules_block(modules)}test ${{#modules[@]}} -gt 0
selected=$(IFS=,; printf '%s' "${{modules[*]}}")
{_install_setup_block()}./mvnw {MAVEN_FLAGS} $flatten_goal install -pl "$install_selected" -am {PREPARE_SKIP_FLAGS}

agent_version=$({BYTE_BUDDY_VERSION_CMD})
test -n "$agent_version"
if [ ! -f "{BYTE_BUDDY_AGENT_JAR}" ]; then
  ./mvnw {MAVEN_FLAGS} dependency:get -Dartifact=net.bytebuddy:byte-buddy-agent:$agent_version
fi

"{JAVA_HOME}/bin/javac" -version
./mvnw -v > /dev/null
{FLATC_PATH} --version
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -n "$(find apm-protocol/apm-network/src/main/proto -name '*.proto' -print -quit)"
test -n "$(find oap-server/server-library/library-banyandb-client/src/main/proto -name '*.proto' -print -quit)"
test -f "{BYTE_BUDDY_AGENT_JAR}"
test -n "$(find /root/.m2/repository -name '*.jar' -print -quit)"
for module in "${{modules[@]}}"; do
  test -d "$module/target/classes"
done
"""


class SkywalkingImageBase14122To14013(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-14122_to_14013"

    def workdir(self) -> str:
        return "base-14122_to_14013"

    def files(self) -> list[File]:
        return [File(".", "settings.xml", SETTINGS_XML)]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make unzip xz-utils cmake g++ {JDK_PACKAGE} \\
    && rm -rf /var/lib/apt/lists/*

RUN ln -sfn "/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)" {JAVA_HOME} \\
    && test -x "{JAVA_HOME}/bin/javac"

ENV JAVA_HOME={JAVA_HOME} \\
    PATH={JAVA_HOME}/bin:$PATH \\
    LC_ALL=C.UTF-8 \\
    CI=true \\
    MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=1g" \\
    MVNW_REPOURL={MAVEN_CENTRAL_MIRROR}

RUN curl -fsSL -o /tmp/flatbuffers.tar.gz \\
    https://github.com/google/flatbuffers/archive/refs/tags/v{FLATC_VERSION}.tar.gz \\
    && printf '%s  /tmp/flatbuffers.tar.gz\\n' {FLATC_SHA256} | sha256sum -c - \\
    && tar -xzf /tmp/flatbuffers.tar.gz -C /tmp \\
    && cd /tmp/flatbuffers-{FLATC_VERSION} \\
    && cmake -G 'Unix Makefiles' -DCMAKE_BUILD_TYPE=Release \\
       -DFLATBUFFERS_BUILD_TESTS=OFF -DFLATBUFFERS_BUILD_FLATLIB=OFF \\
       -DFLATBUFFERS_BUILD_FLATHASH=OFF -DCMAKE_CXX_FLAGS=-Wno-error . \\
    && make -j"$(nproc)" flatc \\
    && install -m 0755 flatc {FLATC_PATH} \\
    && cd / && rm -rf /tmp/flatbuffers-{FLATC_VERSION} /tmp/flatbuffers.tar.gz \\
    && {FLATC_PATH} --version

RUN mkdir -p /root/.m2
COPY settings.xml /root/.m2/settings.xml

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} \\
    && cd /home/{self.pr.repo} \\
    && git submodule update --init --recursive \\
    && git rev-parse HEAD > /dev/null

RUN cd /home/{self.pr.repo} \\
    && (./mvnw {MAVEN_FLAGS} install -DskipTests || true) \\
    && git reset --hard \\
    && git clean -fdx \\
    && ./mvnw -v > /dev/null \\
    && test -n "$(find /root/.m2/repository -name '*.jar' -print -quit)"

{self.clear_env}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class SkywalkingImageDefault14122To14013(Image):
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
        return SkywalkingImageBase14122To14013(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_commands = _test_commands(self.pr)
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

git rev-parse --is-inside-work-tree > /dev/null 2>&1
test -z "$(git status --porcelain)"

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git config core.autocrlf input
git config core.filemode false
git reset --hard
git clean -fdx
git submodule sync --recursive
git submodule update --init --recursive --force
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
git submodule sync --recursive
git submodule update --init --recursive --force
bash /home/check_git_changes.sh

{prepare}
""".format(pr=self.pr, prepare=_prepare_commands(self.pr)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}

{commands}
""".format(pr=self.pr, commands=test_commands),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{commands}
""".format(pr=self.pr, commands=test_commands),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{commands}
""".format(pr=self.pr, commands=test_commands),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register("apache", "skywalking_14122_to_14013")
class SKYWALKING_14122_TO_14013(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SkywalkingImageDefault14122To14013(self.pr, self._config)

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
        ansi_escape_pattern = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        test_log = ansi_escape_pattern.sub("", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        surefire_pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), "
            r"Time elapsed: [\d.,]+ s(?:ec)?(?: <<< (?:FAILURE|ERROR)!)? -+ in (\S+)"
        )

        for line in test_log.splitlines():
            match = surefire_pattern.search(line)
            if not match:
                continue

            tests_run = int(match.group(1))
            failures = int(match.group(2))
            errors = int(match.group(3))
            skipped = int(match.group(4))
            test_name = match.group(5)

            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(test_name)
            elif tests_run > 0:
                passed_tests.add(test_name)

        failed_tests |= _compile_failed_test_classes(test_log)

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
