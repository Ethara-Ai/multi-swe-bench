import base64
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GRADLE_CMD = "./gradlew :test --no-daemon --console=plain --no-build-cache --continue"

_GRADLE_WARMUP_CMD = (
    "./gradlew :testClasses :jmhClasses --no-daemon --console=plain --no-build-cache"
)

_GRADLE_GATE_CMD = "./gradlew :testClasses :jmhClasses --offline --no-daemon --console=plain --no-build-cache"

_GRADLE_INIT_GRADLE = """\
gradle.allprojects { project ->
    project.afterEvaluate {
        project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { task ->
            task.outputs.upToDateWhen { false }
            task.testLogging { logging ->
                logging.events 'passed', 'failed', 'skipped'
                logging.showStandardStreams = false
                logging.exceptionFormat 'short'
            }
        }
    }
}
"""

_GRADLE_INIT_B64 = base64.b64encode(_GRADLE_INIT_GRADLE.encode()).decode()

_JVM_PROXY_SETUP = """\
set -e
p="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
[ -n "$p" ] || exit 0
hp="${p#*://}"
hp="${hp##*@}"
hp="${hp%%/*}"
case "$hp" in
    *:*) host="${hp%:*}"; port="${hp##*:}" ;;
    *) host="$hp"; port=80 ;;
esac
np="$(printf '%s' "${no_proxy:-${NO_PROXY:-}}" | tr ',' '|')"
props=/root/.gradle/gradle.properties
for scheme in http https; do
    echo "systemProp.$scheme.proxyHost=$host" >> "$props"
    echo "systemProp.$scheme.proxyPort=$port" >> "$props"
done
echo "systemProp.http.nonProxyHosts=$np" >> "$props"
[ -s "${CA_CERT_PATH}" ] || exit 0
d="$(mktemp -d)"
awk -v d="$d" '/-----BEGIN CERTIFICATE-----/ {n++; f = d "/" n ".pem"} f {print > f} /-----END CERTIFICATE-----/ {close(f); f = ""}' "${CA_CERT_PATH}"
for c in "$d"/*.pem; do
    [ -e "$c" ] || continue
    keytool -importcert -noprompt -cacerts -storepass changeit -alias "proxy-ca-$(basename "$c" .pem)" -file "$c" > /dev/null
done
rm -rf "$d"
"""

_JVM_PROXY_B64 = base64.b64encode(_JVM_PROXY_SETUP.encode()).decode()

_KOTLIN_CHECK_CMD = (
    "./gradlew :compileTestKotlin --no-daemon --console=plain --no-build-cache"
)

_TEST_BODY = """
rm -rf build/classes-jacoco

set +e
{gradle_cmd} > /tmp/gradle.out 2>&1
GRADLE_RC=$?
set -e

cat /tmp/gradle.out

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "NOTE: gradle exited $GRADLE_RC; see the task results above"
fi

grep -q "^> Task :" /tmp/gradle.out
"""


class GraphqlJavaImageBase(Image):
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
        return "eclipse-temurin:25-jdk-noble"

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

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN mkdir -p /root/.gradle && \\
    echo "org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8" > /root/.gradle/gradle.properties && \\
    echo "org.gradle.daemon=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.parallel=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.caching=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.configureondemand=false" >> /root/.gradle/gradle.properties

RUN echo "{_GRADLE_INIT_B64}" | base64 -d > /root/.gradle/init.gradle

RUN echo "{_JVM_PROXY_B64}" | base64 -d | sh

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GraphqlJavaImageDefault(Image):
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
        return GraphqlJavaImageBase(self.pr, self._config)

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

export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew

{gradle_warmup_cmd} || true

{gradle_gate_cmd} && echo DEPS_OK
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    gradle_warmup_cmd=_GRADLE_WARMUP_CMD,
                    gradle_gate_cmd=_GRADLE_GATE_CMD,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
""".format(repo=self.pr.repo)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

if ! {kotlin_check_cmd} > /tmp/kotlin-check.log 2>&1; then
    git apply --summary /home/test.patch \\
        | sed -n 's/^ create mode [0-7]* //p' \\
        | grep -E '^src/test/kotlin/.*\\.kt$' \\
        | while read -r added; do
            echo "NOTE: removing test-patch Kotlin source that does not compile before the fix: $added"
            rm -f "$added"
        done || true
fi
""".format(repo=self.pr.repo, kotlin_check_cmd=_KOTLIN_CHECK_CMD)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(repo=self.pr.repo)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
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

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_TEST_LINE = re.compile(r"^(\S.* > .+?) (PASSED|FAILED|SKIPPED)$")

_OBJECT_HASH = re.compile(r"@[0-9a-f]{4,}\b")


def parse_gradle_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    seen: dict[str, int] = {}

    clean = ANSI_ESCAPE.sub("", log)

    for raw in clean.splitlines():
        line = raw.rstrip()

        if line.startswith("> "):
            continue

        m = _TEST_LINE.match(line)
        if m:
            name, status = _OBJECT_HASH.sub("@HASH", m.group(1)), m.group(2)
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [dup {seen[name]}]"
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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


@Instance.register("graphql-java", "graphql-java")
class GraphqlJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return GraphqlJavaImageDefault(self.pr, self._config)

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
        return parse_gradle_log(test_log)
