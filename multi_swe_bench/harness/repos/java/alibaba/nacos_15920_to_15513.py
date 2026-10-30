import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_VERSION = "22.22.3"

_MAVEN_BASE_FLAGS = (
    "-B -V --no-transfer-progress '-Prelease-nacos,!dev' "
    "-Dcheckstyle.skip=true -Drat.skip=true "
    "-Dmaven.javadoc.skip=true -Dmaven.source.skip=true"
)

# Back off and retry instead of failing the stage when Maven Central answers 429
# or a 5xx. Both the native and the wagon transport property names are set:
# Maven 3.9 can be driven with either, and an unrecognized -D is ignored.
_MAVEN_RETRY_FLAGS = (
    "-Daether.connector.http.retryHandler.count=5 "
    "-Daether.connector.http.retryHandler.serviceUnavailable=429,500,502,503,504 "
    "-Daether.connector.http.retryHandler.interval=5000 "
    "-Daether.connector.http.retryHandler.intervalMax=60000 "
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.class=standard "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.count=5 "
    "-Dmaven.wagon.http.serviceUnavailableRetryStrategy.retryInterval=5000"
)

MAVEN_FLAGS = f"{_MAVEN_BASE_FLAGS} {_MAVEN_RETRY_FLAGS}"

# repo.maven.apache.org rate-limits a shared build host hard enough to answer
# 429 to a single HEAD, which fails resolution outright. Route `central` at its
# content-identical Google Cloud Storage mirror, which is not rate-limited, so
# both image builds and evaluation stages resolve reliably. Only `central` is
# mirrored -- any other repository the pom declares still resolves directly.
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

# Surefire resolves its provider JAR at the moment it first executes tests, so
# `install -DskipTests` never caches it and every evaluation stage fetched it
# from Maven Central live. Batched runs get throttled to HTTP 429, surefire dies
# before writing any JUnit XML, and the grader sees a (0, 0, 0) stage and voids
# the instance. Seed the provider into the base image so no evaluation stage
# needs the network for it; read the version from the pom so the seed always
# matches the plugin that will actually run.
SUREFIRE_PROVIDER = "org.apache.maven.surefire:surefire-junit-platform"
SUREFIRE_VERSION_CMD = (
    "sed -n 's:.*<maven-surefire-plugin.version>\\(.*\\)"
    "</maven-surefire-plugin.version>.*:\\1:p' pom.xml | head -n 1"
)
SUREFIRE_PROVIDER_JAR = (
    "$HOME/.m2/repository/org/apache/maven/surefire/surefire-junit-platform/"
    "$surefire_version/surefire-junit-platform-$surefire_version.jar"
)

FRONTEND_DIR = "console-ui-next"

NON_UNIT_TEST_PREFIXES = ("test/", "console-ui/", "console-ui-next/")


TEST_SOURCE_ROOT = "/src/test/java/"

# `[ERROR] /home/<repo>/<module>/src/test/java/<pkg>/<Class>.java:[line,col] ...`
# javac's own diagnostic form. Surefire's failure lines carry no absolute path
# and no [line,col], so this cannot match a mere test failure.
COMPILE_ERROR_TEST_FILE = re.compile(
    r"^\[ERROR\]\s+(\S+" + re.escape(TEST_SOURCE_ROOT) + r"\S+\.java):\[\d+,\d+\]"
)


def _patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch or "", re.MULTILINE)


def _compile_failed_test_classes(test_log: str) -> set[str]:
    """Test classes javac refused to compile, as fully-qualified class names.

    A test patch that calls API the fix patch introduces makes the *test
    sources* fail to compile. Maven then aborts the module at test-compile,
    surefire never runs, and the stage parses as (0, 0, 0) -- so the grader
    sees no failing test to transition and credits the instance with nothing
    to fix, even though the pre-fix tree demonstrably cannot run the test.
    Report each class javac named as failed: before the fix it does not pass,
    which is precisely what this stage exists to establish. Classes in the
    same module that merely never ran are deliberately left out, so they stay
    NONE and classify as p2p off their baseline result.
    """
    classes = set()
    for line in test_log.splitlines():
        match = COMPILE_ERROR_TEST_FILE.match(line.strip())
        if not match:
            continue
        rel = match.group(1).split(TEST_SOURCE_ROOT, 1)[1]
        classes.add(rel[: -len(".java")].replace("/", "."))
    return classes


def _java_test_modules(pr: PullRequest) -> list[str]:
    modules = set()
    for path in _patch_files(pr.test_patch):
        if "/src/" not in path or path.startswith(NON_UNIT_TEST_PREFIXES):
            continue
        modules.add(path.split("/src/")[0])
    return sorted(modules)


def _has_frontend_tests(pr: PullRequest) -> bool:
    return any(
        path.startswith(f"{FRONTEND_DIR}/") for path in _patch_files(pr.test_patch)
    )


def _existing_modules_block(modules: list[str]) -> str:
    return f"""modules=()
for module in {" ".join(modules)}; do
  if [ -f "$module/pom.xml" ]; then
    modules+=("$module")
  fi
done
"""


def _test_commands(pr: PullRequest) -> str:
    sections = ["status=0\n"]

    modules = _java_test_modules(pr)
    if modules:
        sections.append(
            f"""cd /home/{pr.repo}
{_existing_modules_block(modules)}if [ ${{#modules[@]}} -gt 0 ]; then
  selected=$(IFS=,; printf '%s' "${{modules[*]}}")
  if ./mvnw {MAVEN_FLAGS} install -pl "$selected" -am -Dmaven.test.skip=true; then
    if ! ./mvnw {MAVEN_FLAGS} test -pl "$selected" -fae -Dmaven.test.failure.ignore=true -Dsurefire.useFile=false -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false; then
      status=1
    fi
  else
    status=1
  fi
fi
"""
        )

    if _has_frontend_tests(pr):
        sections.append(
            f"""cd /home/{pr.repo}/{FRONTEND_DIR}
npm ci --no-audit --no-fund --prefer-offline
if ! npx vitest --run --reporter=verbose; then
  status=1
fi
"""
        )

    sections.append('exit "$status"\n')
    return "\n".join(sections)


def _prepare_commands(pr: PullRequest) -> str:
    sections = []

    modules = _java_test_modules(pr)
    if modules:
        sections.append(
            f"""cd /home/{pr.repo}
./mvnw {MAVEN_FLAGS} install -DskipTests

surefire_version=$({SUREFIRE_VERSION_CMD})
test -n "$surefire_version"
if [ ! -f "{SUREFIRE_PROVIDER_JAR}" ]; then
  ./mvnw {MAVEN_FLAGS} dependency:get -Dartifact={SUREFIRE_PROVIDER}:$surefire_version
fi
test -f "{SUREFIRE_PROVIDER_JAR}"
"""
        )

    if _has_frontend_tests(pr):
        sections.append(
            f"""cd /home/{pr.repo}/{FRONTEND_DIR}
npm ci --no-audit --no-fund
"""
        )

    if modules:
        sections.append(
            f"""cd /home/{pr.repo}
agent_version=$(sed -n 's:.*<byte-buddy-agent.version>\\(.*\\)</byte-buddy-agent.version>.*:\\1:p' pom.xml | head -n 1)
test -n "$agent_version"
test -f "$HOME/.m2/repository/net/bytebuddy/byte-buddy-agent/$agent_version/byte-buddy-agent-$agent_version.jar"
{_existing_modules_block(modules)}for module in "${{modules[@]}}"; do
  compgen -G "$module/target/*.jar" > /dev/null
done
"""
        )

    if _has_frontend_tests(pr):
        sections.append(
            f"""cd /home/{pr.repo}/{FRONTEND_DIR}
node_modules/.bin/vitest --version > /dev/null
"""
        )

    return "\n".join(sections)


class NacosImageBase15920To15513(Image):
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
        return "base-15920-to-15513"

    def workdir(self) -> str:
        return "base-15920-to-15513"

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
RUN apt-get update && apt-get install -y --no-install-recommends gnupg ca-certificates git curl unzip xz-utils && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg && printf 'deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main\\n' > /etc/apt/sources.list.d/zulu.list
RUN apt-get update && apt-get install -y --no-install-recommends zulu17-jdk && rm -rf /var/lib/apt/lists/*
RUN ARCH="$(dpkg --print-architecture)" && if [ "$ARCH" = "amd64" ]; then NODE_ARCH=x64; else NODE_ARCH=arm64; fi && curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$NODE_ARCH.tar.gz" -o /tmp/node.tar.gz && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 && rm /tmp/node.tar.gz
RUN git config --global --add safe.directory '*'
ENV JAVA_HOME=/usr/lib/jvm/zulu17 \\
    PATH=/usr/lib/jvm/zulu17/bin:$PATH \\
    LC_ALL=C.UTF-8 \\
    CI=true \\
    MAVEN_OPTS="-Xmx4g" \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"

COPY settings.xml /root/.m2/settings.xml

ENV MVNW_REPOURL={MAVEN_CENTRAL_MIRROR}

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

RUN cd /home/{self.pr.repo} \\
 && surefire_version=$({SUREFIRE_VERSION_CMD}) \\
 && test -n "$surefire_version" \\
 && ./mvnw {MAVEN_FLAGS} dependency:get -Dartifact={SUREFIRE_PROVIDER}:$surefire_version \\
 && test -f "{SUREFIRE_PROVIDER_JAR}"

{self.clear_env}

CMD ["/bin/bash"]
"""


class NacosImageDefault15920To15513(Image):
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
        return NacosImageBase15920To15513(self.pr, self._config)

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
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
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

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

"""


@Instance.register("alibaba", "nacos_15920_to_15513")
class NACOS_15920_TO_15513(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return NacosImageDefault15920To15513(self.pr, self._config)

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
        vitest_pattern = re.compile(r"^\s*(✓|√|×|✗|↓|FAIL)\s+(.+?)\s*$")
        vitest_suffix_pattern = re.compile(
            r"(?:\s+\[[^\]]*\])*(?:\s+\(retry x\d+\))?(?:\s+\d+(?:\.\d+)?m?s)?$"
        )

        for line in test_log.splitlines():
            match = surefire_pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5)

                if failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)
                elif tests_run > 0:
                    passed_tests.add(test_name)
                continue

            match = vitest_pattern.match(line)
            if match:
                status = match.group(1)
                test_name = vitest_suffix_pattern.sub("", match.group(2)).strip()
                if " > " not in test_name:
                    continue

                if status in ("×", "✗", "FAIL"):
                    failed_tests.add(test_name)
                elif status == "↓":
                    skipped_tests.add(test_name)
                else:
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
