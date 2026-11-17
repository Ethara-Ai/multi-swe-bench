import base64
import os
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "redisson"
_REPO = "redisson"
_INTERVAL = "redisson_7390_to_7112"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_REDIS_VERSION = "8.10.3"

_MVN = "mvn -B -ntp -Dstyle.color=never"

_MVN_TEST = (
    f"{_MVN} -fae clean test "
    "-Dmaven.test.skip=false "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dsurefire.rerunFailingTestsCount=3 "
    "-pl {modules} -am "
    "-Dtest={tests}"
)

_RESULTS_MARKER = "##TEST_RESULTS##"

_SHELL_ENV = """\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export MAVEN_OPTS=-Xmx3g
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
mkdir -p /root/.m2
{
    echo "<settings><proxies>"
    for scheme in http https; do
        echo "<proxy><id>$scheme</id><active>true</active><protocol>$scheme</protocol><host>$host</host><port>$port</port><nonProxyHosts>$np</nonProxyHosts></proxy>"
    done
    echo "</proxies></settings>"
} > /root/.m2/settings.xml
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

_LOCAL_REDIS_SERVER_JAVA = """\
package org.redisson;

public final class LocalRedisServer {

    private static final int PORT = launch();

    private LocalRedisServer() {
    }

    public static int port() {
        return PORT;
    }

    private static int launch() {
        try {
            int port;
            try (java.net.ServerSocket socket = new java.net.ServerSocket(0)) {
                port = socket.getLocalPort();
            }
            java.nio.file.Path dir = java.nio.file.Files.createTempDirectory("local-redis");
            Process process = new ProcessBuilder(
                    "redis-server",
                    "--port", String.valueOf(port),
                    "--bind", "127.0.0.1",
                    "--save", "",
                    "--appendonly", "no",
                    "--dir", dir.toString())
                    .redirectErrorStream(true)
                    .redirectOutput(dir.resolve("redis.log").toFile())
                    .start();
            Runtime.getRuntime().addShutdownHook(new Thread(process::destroy));
            long deadline = System.currentTimeMillis() + 30000;
            while (true) {
                try (java.net.Socket probe = new java.net.Socket("127.0.0.1", port)) {
                    return port;
                } catch (java.io.IOException e) {
                    if (!process.isAlive() || System.currentTimeMillis() > deadline) {
                        throw new IllegalStateException("local redis-server failed to start", e);
                    }
                    Thread.sleep(50);
                }
            }
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
"""

_LOCAL_REDIS_FUNC = (
    "local_redis() {\n"
    "    for m in \"$@\"; do\n"
    "        f=\"$(find \"$m/src/test/java\" -name RedisDockerTest.java 2>/dev/null | sort | head -n 1 || true)\"\n"
    "        [ -n \"$f\" ] || continue\n"
    "        pkg=\"$(sed -n 's/^package \\([^;]*\\);.*/\\1/p' \"$f\" | head -n 1)\"\n"
    "        cat > \"$(dirname \"$f\")/LocalRedisServer.java\" <<'LOCALREDISEOF'\n"
    + _LOCAL_REDIS_SERVER_JAVA
    + "LOCALREDISEOF\n"
    "        sed -i \"s/^package [^;]*;/package $pkg;/\" \"$(dirname \"$f\")/LocalRedisServer.java\"\n"
    "        sed -i -E \\\n"
    "            -e 's/\\b(CONTAINER|REDIS)\\.start\\(\\);/LocalRedisServer.port();/g' \\\n"
    "            -e 's/\\b(CONTAINER|REDIS)\\.stop\\(\\);//g' \\\n"
    "            -e 's|\"redis://127\\.0\\.0\\.1:\" \\+ container\\.getFirstMappedPort\\(\\)|\"redis://127.0.0.1:\" + LocalRedisServer.port()|g' \\\n"
    "            \"$f\"\n"
    "        if ! grep -q 'LocalRedisServer.port()' \"$f\"; then\n"
    "            echo \"local_redis: rewrite failed for $f\"\n"
    "            exit 1\n"
    "        fi\n"
    "        echo \"local_redis: $f uses a local redis-server\"\n"
    "    done\n"
    "}"
)

_ROUTING_FLAG = "_redisson_7390_to_7112_routed"

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


def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch or "", flags=re.MULTILINE)


def _modules(pr: PullRequest) -> str:
    found = set()
    for path in _patch_paths(pr.fix_patch) + _patch_paths(pr.test_patch):
        if "/src/" in path:
            found.add(path.split("/src/", 1)[0])
    return ",".join(sorted(found)) or "redisson"


def _tests(pr: PullRequest) -> str:
    found = set()
    for path in _patch_paths(pr.test_patch):
        if re.search(r"/src/test/java/.+\.java$", path):
            name = os.path.basename(path)[: -len(".java")]
            if "Test" in name:
                found.add(name)
    return ",".join(sorted(found))


_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
{apply}local_redis {module_args}
find /home/{repo} -type d -path '*/target/surefire-reports' -prune -exec rm -rf {{}} +
{test_step}
results="$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (maven exit code $status)"
    exit 1
fi
while read -r f; do
    echo "{marker} $f"
    sed '/<properties>/,/<\\/properties>/d' "$f"
    echo
done <<< "$results"
exit "$status"
"""

_TEST_STEP = """status=0
{mvn_test} 2>&1 || status=$?"""

_TEST_STEP_EXCLUDING_UNCOMPILABLE = """log="$(mktemp)"
for attempt in 1 2 3 4 5; do
    status=0
    {mvn_test} 2>&1 | tee "$log" || status=$?
    broken="$(grep -E '^\\[ERROR\\] /home/{repo}/[^:]*/src/test/java/[^:]*\\.java:' "$log" \\
        | grep -oE '/home/{repo}/[^:]*/src/test/java/[^:]*\\.java' \\
        | sort -u || true)"
    if [ -z "$broken" ]; then
        break
    fi
    while read -r f; do
        echo "Excluding uncompilable test source: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done <<< "$broken"
done"""


class Redisson7390To7112ImageBase(Image):
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
        return "maven:3.9.16-eclipse-temurin-25"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    mkdir -p /tmp/redis-src; \\
    curl -fsSL "https://download.redis.io/releases/redis-{_REDIS_VERSION}.tar.gz" \\
        | tar -xz -C /tmp/redis-src --strip-components=1; \\
    make -C /tmp/redis-src/src -j"$(nproc)" BUILD_TLS=no redis-server redis-cli; \\
    install -m 0755 /tmp/redis-src/src/redis-server /tmp/redis-src/src/redis-cli /usr/local/bin/; \\
    rm -rf /tmp/redis-src; \\
    redis-server --version

RUN git config --global --add safe.directory '*'

RUN echo "{_JVM_PROXY_B64}" | base64 -d | sh

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Redisson7390To7112ImageDefault(Image):
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
        return Redisson7390To7112ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        modules = _modules(self.pr)
        module_args = " ".join(modules.split(","))
        mvn_test = _MVN_TEST.format(modules=modules, tests=_tests(self.pr))
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
{shell_env}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

local_redis {module_args}

{mvn_test}

git checkout -- .
git clean -fd
test "$(git rev-parse HEAD)" = "{sha}"
command -v redis-server
redis-server --version

{mvn} -o test-compile -Dmaven.test.skip=false -Dmaven.javadoc.skip=true -pl {modules} -am
echo DEPS_OK
""".format(
                    shell_env=_SHELL_ENV + "\n" + _LOCAL_REDIS_FUNC,
                    repo=repo,
                    sha=self.pr.base.sha,
                    mvn=_MVN,
                    mvn_test=mvn_test,
                    modules=modules,
                    module_args=module_args,
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV + "\n" + _LOCAL_REDIS_FUNC,
                    repo=repo,
                    apply="",
                    module_args=module_args,
                    test_step=_TEST_STEP.format(mvn_test=mvn_test),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV + "\n" + _LOCAL_REDIS_FUNC,
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch\n",
                    module_args=module_args,
                    test_step=_TEST_STEP_EXCLUDING_UNCOMPILABLE.format(
                        mvn_test=mvn_test, repo=repo
                    ),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV + "\n" + _LOCAL_REDIS_FUNC,
                    repo=repo,
                    apply="git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                    module_args=module_args,
                    test_step=_TEST_STEP.format(mvn_test=mvn_test),
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
class REDISSON_7390_TO_7112(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Redisson7390To7112ImageDefault(self.pr, self._config)

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
            prefix = f"{rel.split('/target/', 1)[0]}/" if "/target/" in rel else ""
            for match in testcase_pattern.finditer(content):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                classname = classname_pattern.search(attrs)
                if not name or not classname:
                    continue

                top_level, _, nested = classname.group(1).partition("$")
                scope = "".join(f"{part}::" for part in nested.split("$") if part)
                test_name = (
                    f"{prefix}src/test/java/{top_level.replace('.', '/')}.java"
                    f"::{scope}{name.group(1)}"
                )
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
            elif "skipped" in seen:
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
