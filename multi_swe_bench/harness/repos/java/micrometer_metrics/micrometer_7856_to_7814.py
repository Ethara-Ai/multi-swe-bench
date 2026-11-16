import base64
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "micrometer-metrics"
_REPO = "micrometer"
_INTERVAL = "micrometer_7856_to_7814"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_GRADLE = (
    "./gradlew --no-daemon --no-scan --no-build-cache --no-configuration-cache "
    "--console=plain --max-workers=2 -Prelease.version=1.17.0"
)

_RESULTS_MARKER = "##TEST_RESULTS##"

_SHELL_ENV = """\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export GRADLE_USER_HOME=/root/.gradle
p="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
if [ -n "$p" ]; then
    hp="${p#*://}"
    hp="${hp##*@}"
    hp="${hp%%/*}"
    case "$hp" in
        *:*) ph="${hp%:*}"; pp="${hp##*:}" ;;
        *) ph="$hp"; pp=80 ;;
    esac
    np="$(printf '%s' "${no_proxy:-${NO_PROXY:-}}" | tr ',' '|' | sed -e 's/^\\./*./' -e 's/|\\./|*./g')"
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} -Dhttp.proxyHost=$ph -Dhttp.proxyPort=$pp -Dhttps.proxyHost=$ph -Dhttps.proxyPort=$pp -Dhttp.nonProxyHosts=$np"
fi"""

_GRADLE_PROPS_SETUP = """\
mkdir -p /root/.gradle
touch /root/.gradle/gradle.properties
sed -i -e '/^org.gradle.jvmargs=/d' -e '/^org.gradle.java.installations.auto-download=/d' -e '/^org.gradle.caching=/d' -e '/^org.gradle.vfs.watch=/d' -e '/^org.gradle.parallel=/d' -e '/^org.gradle.workers.max=/d' -e '/^org.gradle.configuration-cache=/d' /root/.gradle/gradle.properties
{
    echo "org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -XX:+HeapDumpOnOutOfMemoryError"
    echo "org.gradle.java.installations.auto-download=false"
    echo "org.gradle.caching=false"
    echo "org.gradle.vfs.watch=false"
    echo "org.gradle.parallel=true"
    echo "org.gradle.workers.max=2"
    echo "org.gradle.configuration-cache=false"
} >> /root/.gradle/gradle.properties"""

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
mkdir -p /root/.m2 /root/.gradle
{
    echo "<settings><proxies>"
    for scheme in http https; do
        echo "<proxy><id>$scheme</id><active>true</active><protocol>$scheme</protocol><host>$host</host><port>$port</port><nonProxyHosts>$np</nonProxyHosts></proxy>"
    done
    echo "</proxies></settings>"
} > /root/.m2/settings.xml
touch /root/.gradle/gradle.properties
sed -i -e '/^systemProp.http.proxyHost=/d' -e '/^systemProp.http.proxyPort=/d' -e '/^systemProp.https.proxyHost=/d' -e '/^systemProp.https.proxyPort=/d' -e '/^systemProp.http.nonProxyHosts=/d' -e '/^systemProp.https.nonProxyHosts=/d' /root/.gradle/gradle.properties
{
    echo "systemProp.http.proxyHost=$host"
    echo "systemProp.http.proxyPort=$port"
    echo "systemProp.https.proxyHost=$host"
    echo "systemProp.https.proxyPort=$port"
    echo "systemProp.http.nonProxyHosts=$np"
    echo "systemProp.https.nonProxyHosts=$np"
} >> /root/.gradle/gradle.properties
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

_ROUTING_FLAG = "_micrometer_metrics_micrometer_7856_to_7814_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)


def _touched_projects(pr: PullRequest) -> list[str]:
    projects: set[str] = set()
    for patch in (pr.fix_patch, pr.test_patch):
        for path in re.findall(r"^diff --git a/(\S+) b/", patch or "", re.MULTILINE):
            parts = path.split("/")
            if parts[0] in ("implementations", "samples") and len(parts) > 2:
                name, rest = parts[1], parts[2:]
            elif parts[0] == "benchmarks" and len(parts) > 2:
                name, rest = f"micrometer-{parts[1]}", parts[2:]
            elif len(parts) > 1:
                name, rest = parts[0], parts[1:]
            else:
                continue
            if rest and rest[0] == "src":
                projects.add(f":{name}")
    return sorted(projects)


def _test_tasks(pr: PullRequest) -> str:
    return " ".join(f"{p}:cleanTest {p}:test" for p in _touched_projects(pr))


def _compile_tasks(pr: PullRequest) -> str:
    return " ".join(f"{p}:testClasses" for p in _touched_projects(pr))


_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
{apply}find /home/{repo} -type d -path '*/build/test-results' -prune -exec rm -rf {{}} +
{test_step}
results="$(find . -path '*/build/test-results/test/TEST-*.xml' 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (gradle exit code $status)"
    exit 1
fi
while read -r f; do
    echo "{marker} $f"
    grep -E '^[[:space:]]*<(testcase|/testcase|failure|error|skipped)' "$f" \\
        | sed -E 's#^([[:space:]]*<(failure|error|skipped)).*#\\1/>#' || true
    echo
done <<< "$results"
exit "$status"
"""

_TEST_STEP = """status=0
{gradle} --continue {tasks} 2>&1 || status=$?"""



class Micrometer7856To7814ImageBase(Image):
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
        return "eclipse-temurin:21.0.8_9-jdk"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
            )
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

RUN echo "{_JVM_PROXY_B64}" | base64 -d | sh

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Micrometer7856To7814ImageDefault(Image):
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
        return Micrometer7856To7814ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_tasks = _test_tasks(self.pr)
        compile_tasks = _compile_tasks(self.pr)
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
set -eo pipefail
{shell_env}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{gradle_props}

for i in 1 2 3; do
    ./gradlew --no-daemon --version && break
    [ "$i" = 3 ] && exit 1
    sleep 15
done

for i in 1 2 3; do
    {gradle} {compile_tasks} && break
    [ "$i" = 3 ] && exit 1
    sleep 30
done

{gradle} --continue {test_tasks} || true

{gradle} --offline {compile_tasks}
echo DEPS_OK
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    gradle_props=_GRADLE_PROPS_SETUP,
                    gradle=_GRADLE,
                    compile_tasks=compile_tasks,
                    test_tasks=test_tasks,
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply="",
                    test_step=_TEST_STEP.format(gradle=_GRADLE, tasks=test_tasks),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch\n",
                    test_step=_TEST_STEP.format(gradle=_GRADLE, tasks=test_tasks),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                    test_step=_TEST_STEP.format(gradle=_GRADLE, tasks=test_tasks),
                    marker=_RESULTS_MARKER,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register(_ORG, _INTERVAL)
class MICROMETER_7856_TO_7814(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Micrometer7856To7814ImageDefault(self.pr, self._config)

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
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        clean = re.sub(r"<!\[CDATA\[.*?\]\]>", "", clean, flags=re.DOTALL)

        chunks = re.split(
            rf"^{re.escape(_RESULTS_MARKER)} (\S+)\s*$", clean, flags=re.MULTILINE
        )

        testcase_pattern = re.compile(
            r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL
        )
        name_pattern = re.compile(r'\bname="([^"]*)"')
        classname_pattern = re.compile(r'\bclassname="([^"]*)"')

        statuses: dict[str, set[str]] = {}

        for path, content in zip(chunks[1::2], chunks[2::2]):
            rel = re.sub(r"^\./", "", path)
            prefix = rel.split("/build/test-results/", 1)[0] if "/build/test-results/" in rel else "."
            for match in testcase_pattern.finditer(content):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                classname = classname_pattern.search(attrs)
                if not name or not classname:
                    continue

                test_name = f"{prefix}::{classname.group(1)}::{name.group(1)}"
                body = match.group(3) or ""

                if "<failure" in body or "<error" in body:
                    status = "failed"
                elif "<skipped" in body:
                    status = "skipped"
                else:
                    status = "passed"
                statuses.setdefault(test_name, set()).add(status)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for test_name, seen in statuses.items():
            if "failed" in seen:
                failed_tests.add(test_name)
            elif "passed" in seen:
                passed_tests.add(test_name)
            else:
                skipped_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
