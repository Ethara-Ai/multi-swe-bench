import base64
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "GeyserMC"
_REPO = "Geyser"
_INTERVAL = "Geyser_6786_to_6712"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_BASE_IMAGE = "eclipse-temurin:25-jdk-noble"
_JDK21_IMAGE = "eclipse-temurin:21-jdk-noble"
_JDK21_HOME = "/opt/jdk21"

_INIT_SCRIPT = "/home/geyser-test-init.gradle"

_GRADLE = (
    "./gradlew --no-daemon --no-build-cache --console=plain --max-workers 2 "
    f"--init-script {_INIT_SCRIPT}"
)

_GAMETEST_PROJECT = ":gametest"

_PROJECT_DIRS = {
    "ap": ":ap",
    "api": ":api",
    "common": ":common",
    "core": ":core",
    "bootstrap/bungeecord": ":bungeecord",
    "bootstrap/mod": ":mod",
    "bootstrap/mod/fabric": ":fabric",
    "bootstrap/mod/gametest": _GAMETEST_PROJECT,
    "bootstrap/mod/neoforge": ":neoforge",
    "bootstrap/spigot": ":spigot",
    "bootstrap/standalone": ":standalone",
    "bootstrap/velocity": ":velocity",
    "bootstrap/viaproxy": ":viaproxy",
}

_RESULTS_MARKER = "##TEST_RESULTS##"

_INIT_SCRIPT_CONTENT = """\
gradle.beforeProject { p ->
    p.tasks.withType(JavaExec).configureEach { t ->
        if (t.name == 'runGameTest') {
            t.systemProperty('fabric-api.gametest.report-file', new File(p.projectDir, 'build/test-results/runGameTest/TEST-runGameTest.xml').absolutePath)
        }
    }
}
"""

_SHELL_ENV = """\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export GRADLE_USER_HOME=/root/.gradle
mkdir -p "$GRADLE_USER_HOME"
{
    echo "org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8"
    echo "org.gradle.daemon=false"
    echo "org.gradle.caching=false"
    echo "org.gradle.workers.max=2"
    echo "org.gradle.java.installations.auto-detect=true"
    echo "org.gradle.java.installations.paths=__JDK21__,/opt/java/openjdk"
    echo "systemProp.org.gradle.internal.repository.max.retries=10"
    echo "systemProp.org.gradle.internal.repository.initial.backoff=2000"
    echo "systemProp.org.gradle.internal.http.connectionTimeout=120000"
    echo "systemProp.org.gradle.internal.http.socketTimeout=120000"
} > "$GRADLE_USER_HOME/gradle.properties"
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
    {
        echo "systemProp.http.proxyHost=$ph"
        echo "systemProp.http.proxyPort=$pp"
        echo "systemProp.https.proxyHost=$ph"
        echo "systemProp.https.proxyPort=$pp"
        echo "systemProp.http.nonProxyHosts=$np"
        echo "systemProp.https.nonProxyHosts=$np"
    } >> "$GRADLE_USER_HOME/gradle.properties"
fi""".replace("__JDK21__", _JDK21_HOME)

_JVM_PROXY_SETUP = """\
set -e
p="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
[ -n "$p" ] || exit 0
[ -s "${CA_CERT_PATH}" ] || exit 0
d="$(mktemp -d)"
awk -v d="$d" '/-----BEGIN CERTIFICATE-----/ {n++; f = d "/" n ".pem"} f {print > f} /-----END CERTIFICATE-----/ {close(f); f = ""}' "${CA_CERT_PATH}"
for jdk in /opt/java/openjdk __JDK21__; do
    for c in "$d"/*.pem; do
        [ -e "$c" ] || continue
        "$jdk/bin/keytool" -importcert -noprompt -keystore "$jdk/lib/security/cacerts" -storepass changeit -alias "proxy-ca-$(basename "$c" .pem)" -file "$c" > /dev/null
    done
done
rm -rf "$d"
""".replace("__JDK21__", _JDK21_HOME)

_JVM_PROXY_B64 = base64.b64encode(_JVM_PROXY_SETUP.encode()).decode()

_SYNC_SUBMODULES = """\
sync_submodules() {
    for patch in "$@"; do
        awk '/^diff --git /{path=substr($3,3)} /^\\+Subproject commit /{print path, $3}' "$patch"
    done | while read -r path sha; do
        [ -n "$path" ] && [ -n "$sha" ] || continue
        if ! git -C "$path" cat-file -e "$sha^{commit}" 2>/dev/null; then
            name="$(git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$' | awk -v p="$path" '$2 == p {sub(/^submodule\\./, "", $1); sub(/\\.path$/, "", $1); print $1}')"
            url="$(git config -f .gitmodules --get "submodule.$name.url")"
            test -n "$url"
            fetched=0
            for i in 1 2 3; do
                if git -C "$path" fetch -q "$url" "$sha"; then
                    fetched=1
                    break
                fi
                sleep 10
            done
            test "$fetched" -eq 1
        fi
        git -C "$path" checkout -q --detach "$sha"
        test "$(git -C "$path" rev-parse HEAD)" = "$sha"
    done
}"""

_SHELL_ENV = (
    _SHELL_ENV
    + f"\ncat > {_INIT_SCRIPT} <<'INITGRADLEEOF'\n"
    + _INIT_SCRIPT_CONTENT
    + "INITGRADLEEOF"
)

_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

{sync_submodules}

cd /home/{repo}
{apply}find /home/{repo} -type d -path '*/build/test-results' -prune -exec rm -rf {{}} +
{test_step}
results="$(find . -path '*/build/test-results/*' -name 'TEST-*.xml' 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (gradle exit code $status)"
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
{gradle_test} 2>&1 || status=$?"""

_TEST_STEP_EXCLUDING_UNCOMPILABLE = """log="$(mktemp)"
for attempt in 1 2 3 4 5; do
    status=0
    {gradle_test} 2>&1 | tee "$log" || status=$?
    broken="$(grep -E '^/home/{repo}/[^:]*/src/test/java/[^:]*\\.java:[0-9]+: error:' "$log" \\
        | grep -oE '^/home/{repo}/[^:]*/src/test/java/[^:]*\\.java' \\
        | sort -u || true)"
    if [ -z "$broken" ]; then
        break
    fi
    while read -r f; do
        echo "Excluding uncompilable test source: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done <<< "$broken"
    find /home/{repo} -type d -path '*/build/test-results' -prune -exec rm -rf {{}} +
done"""


def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch or "", flags=re.MULTILINE)


def _project_for(path: str) -> str | None:
    best = None
    for directory, project in _PROJECT_DIRS.items():
        if path.startswith(directory + "/") and (
            best is None or len(directory) > len(best[0])
        ):
            best = (directory, project)
    return best[1] if best else None


def _test_tasks(pr: PullRequest) -> list[str]:
    projects = []
    for path in _patch_paths(pr.test_patch):
        project = _project_for(path)
        if project and project not in projects:
            projects.append(project)
    if not projects:
        projects.append(":core")
    tasks = []
    for project in sorted(projects):
        if project == _GAMETEST_PROJECT:
            tasks.append(f"{project}:runGameTest")
        else:
            tasks.append(f"{project}:test")
    return tasks


def _compile_tasks(pr: PullRequest) -> list[str]:
    tasks = []
    for task in _test_tasks(pr):
        project = task.rsplit(":", 1)[0]
        if project == _GAMETEST_PROJECT:
            tasks.append(f"{project}:classes")
        else:
            tasks.append(f"{project}:testClasses")
    return tasks


class Geyser6786To6712ImageBase(Image):
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
        return _BASE_IMAGE

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

COPY --from={_JDK21_IMAGE} /opt/java/openjdk {_JDK21_HOME}

RUN git config --global --add safe.directory '*'

RUN echo "{_JVM_PROXY_B64}" | base64 -d | sh

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Geyser6786To6712ImageDefault(Image):
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
        return Geyser6786To6712ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        gradle_test = f"{_GRADLE} --continue {' '.join(_test_tasks(self.pr))}"
        gradle_gate = f"{_GRADLE} --offline {' '.join(_compile_tasks(self.pr))}"
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
git clean -ffdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
for i in 1 2 3; do
    git submodule update --init --recursive && break
    [ "$i" = 3 ] && exit 1
    sleep 15
done
bash /home/check_git_changes.sh

for i in 1 2 3; do
    ./gradlew --no-daemon --version && break
    [ "$i" = 3 ] && exit 1
    sleep 15
done

{gradle_test} || true

{gradle_gate}
echo DEPS_OK
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    gradle_test=gradle_test,
                    gradle_gate=gradle_gate,
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    sync_submodules=_SYNC_SUBMODULES,
                    repo=repo,
                    apply="",
                    test_step=_TEST_STEP.format(gradle_test=gradle_test),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    sync_submodules=_SYNC_SUBMODULES,
                    repo=repo,
                    apply=(
                        "git apply --whitespace=nowarn /home/test.patch\n"
                        "sync_submodules /home/test.patch\n"
                    ),
                    test_step=_TEST_STEP_EXCLUDING_UNCOMPILABLE.format(
                        gradle_test=gradle_test, repo=repo
                    ),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    sync_submodules=_SYNC_SUBMODULES,
                    repo=repo,
                    apply=(
                        "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                        "sync_submodules /home/test.patch /home/fix.patch\n"
                    ),
                    test_step=_TEST_STEP.format(gradle_test=gradle_test),
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


_ROUTING_FLAG = "_geysermc_geyser_6786_to_6712_routed"

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


@Instance.register(_ORG, _INTERVAL)
class GEYSER_6786_TO_6712(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Geyser6786To6712ImageDefault(self.pr, self._config)

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
            module, sep, tail = rel.partition("/build/test-results/")
            if not sep:
                module, tail = "", rel
            is_gametest = tail.startswith("runGameTest/")
            for match in testcase_pattern.finditer(content):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                if not name:
                    continue
                if is_gametest:
                    test_name = f"{module}::gametest::{name.group(1)}"
                else:
                    classname = classname_pattern.search(attrs)
                    if not classname:
                        continue
                    top_level, _, nested = classname.group(1).partition("$")
                    scope = "".join(
                        f"{part}::" for part in nested.split("$") if part
                    )
                    test_name = (
                        f"{module}/src/test/java/{top_level.replace('.', '/')}.java"
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
