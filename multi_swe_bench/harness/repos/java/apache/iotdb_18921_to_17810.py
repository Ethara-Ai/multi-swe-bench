import base64
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "apache"
_REPO = "iotdb"
_INTERVAL = "iotdb_18921_to_17810"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_BUILD_ENV = "/home/iotdb_build.env"

_TSFILE_RELEASE_FALLBACK = {
    "2.3.1-260520-SNAPSHOT": "2.3.1",
    "2.3.2-260608-SNAPSHOT": "2.3.1",
    "2.3.2-260721-SNAPSHOT": "2.4.0",
}

_MVN = (
    "mvn -B -ntp -nsu -Dstyle.color=never "
    "-Dspotless.skip=true "
    "-Dcheckstyle.skip=true "
    "-Dxml-format.skip=true "
    "-Drat.skip=true "
    "-Denforcer.skip=true "
    "-Dforbiddenapis.skip=true "
    "-Dmdep.analyze.skip=true "
    "-Djacoco.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.source.skip=true "
    "-Dgpg.skip=true"
)

_MVN_TEST = (
    f'{_MVN} -fae test -pl "$IOTDB_MODULES" -am $IOTDB_PROPS '
    '-Dtest="$IOTDB_TESTS" '
    "-Dtest.port.closed=true "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

_RESULTS_MARKER = "##TEST_RESULTS##"

_SHELL_ENV = """\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"
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

_ROUTING_FLAG = "_apache_iotdb_18921_to_17810_routed"

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


_PREPARE_TEMPLATE = """#!/bin/bash
set -e
{shell_env}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

IOTDB_PROPS=""
tsfile_version="$(sed -n 's|.*<tsfile.version>\\(.*\\)</tsfile.version>.*|\\1|p' pom.xml | head -n 1)"
test -n "$tsfile_version"
echo "prepare: tsfile.version=$tsfile_version"
case "$tsfile_version" in
    *-SNAPSHOT)
        if (cd /tmp && {mvn} -q org.apache.maven.plugins:maven-dependency-plugin:3.6.0:get \\
                -Dartifact="org.apache.tsfile:tsfile:$tsfile_version:pom" \\
                -Dtransitive=false \\
                -DremoteRepositories=apache.snapshots::default::https://repository.apache.org/snapshots); then
            echo "prepare: tsfile snapshot $tsfile_version is resolvable"
        else
            case "$tsfile_version" in
{fallback_cases}
                *) echo "prepare: tsfile snapshot $tsfile_version is unavailable and has no release fallback" >&2; exit 1 ;;
            esac
        fi
        ;;
esac
echo "prepare: extra maven properties: $IOTDB_PROPS"

modules=""
for m in {modules}; do
    [ -f "$m/pom.xml" ] || continue
    if out="$({mvn} -q validate -pl "$m" $IOTDB_PROPS 2>&1)"; then
        modules="${{modules:+$modules,}}$m"
    else
        echo "prepare: $m is not part of the default reactor, skipping"
        printf '%s\\n' "$out" | tail -n 5
    fi
done
test -n "$modules"
echo "prepare: modules=$modules"

{{
    printf 'IOTDB_MODULES=%q\\n' "$modules"
    printf 'IOTDB_PROPS=%q\\n' "$IOTDB_PROPS"
    printf 'IOTDB_TESTS=%q\\n' "{tests}"
}} > {build_env}
cat {build_env}
. {build_env}
test -n "$IOTDB_TESTS"

{mvn} install -DskipTests -pl "$IOTDB_MODULES" -am $IOTDB_PROPS

status=0
{mvn_test} || status=$?
echo "prepare: baseline test run exited with $status"

for m in ${{IOTDB_MODULES//,/ }}; do
    if [ ! -d "$m/target/classes" ]; then
        echo "prepare: module $m was not compiled" >&2
        exit 1
    fi
    if [ -d "$m/src/test/java" ] && [ ! -d "$m/target/test-classes" ]; then
        echo "prepare: tests of module $m were not compiled" >&2
        exit 1
    fi
done
test -n "$(find /root/.m2/repository/org/apache/tsfile/tsfile -name '*.jar' 2>/dev/null | head -n 1)"
test -d /root/.m2/repository/org/apache/maven/surefire
echo DEPS_OK
"""

_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
. {build_env}
{apply}find /home/{repo} -type d -path '*/target/surefire-reports' -prune -exec rm -rf {{}} +
{test_step}
results="$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (maven exit code $status)"
    exit "$status"
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



def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/.+? b/(.+)$", patch, re.M)


class Iotdb18921To17810ImageBase(Image):
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
        return "maven:3.9.12-eclipse-temurin-17"

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


class Iotdb18921To17810ImageDefault(Image):
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
        return Iotdb18921To17810ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _modules(self) -> list[str]:
        paths = _patch_paths(self.pr.fix_patch) + _patch_paths(self.pr.test_patch)
        return sorted({p.split("/src/", 1)[0] for p in paths if "/src/" in p})

    def _tests(self) -> list[str]:
        tests = set()
        for p in _patch_paths(self.pr.test_patch):
            match = re.match(r"^(.+?)/src/test/java/(.+)\.java$", p)
            if not match:
                continue
            cls = match.group(2).rsplit("/", 1)[-1]
            if not re.match(r"^(Test.*|.*Test|.*Tests|.*TestCase)$", cls):
                continue
            tests.add(match.group(2).replace("/", "."))
        return sorted(tests)

    def files(self) -> list[File]:
        repo = self.pr.repo
        fallback_cases = "\n".join(
            f'                {snap}) IOTDB_PROPS="-Dtsfile.version={rel}"; '
            f'echo "prepare: tsfile snapshot {snap} is unavailable, using release {rel}" ;;'
            for snap, rel in _TSFILE_RELEASE_FALLBACK.items()
        )
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
                _PREPARE_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    mvn=_MVN,
                    mvn_test=_MVN_TEST,
                    modules=" ".join(self._modules()),
                    tests=",".join(self._tests()),
                    build_env=_BUILD_ENV,
                    fallback_cases=fallback_cases,
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    build_env=_BUILD_ENV,
                    apply="",
                    test_step=_TEST_STEP.format(mvn_test=_MVN_TEST),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    build_env=_BUILD_ENV,
                    apply="git apply --whitespace=nowarn /home/test.patch\n",
                    test_step=_TEST_STEP.format(mvn_test=_MVN_TEST),
                    marker=_RESULTS_MARKER,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    build_env=_BUILD_ENV,
                    apply="git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                    test_step=_TEST_STEP.format(mvn_test=_MVN_TEST),
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
class IOTDB_18921_TO_17810(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Iotdb18921To17810ImageDefault(self.pr, self._config)

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
