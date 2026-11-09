import base64
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "jetty"
_REPO = "jetty.project"
_INTERVAL = "jetty.project_15857_to_14197"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_MVN = (
    "mvn -B -ntp -Dstyle.color=never"
    " -Dspotless.skip=true -Dspotless.check.skip=true -Dcheckstyle.skip=true"
    " -Dspotbugs.skip=true -Dlicense.skip=true -Djacoco.skip=true"
    " -Dmaven.javadoc.skip=true"
)

_SUREFIRE_FLAGS = (
    "-Dsurefire.enableOutErrElements=false"
    " -Dsurefire.enablePropertiesElement=false"
    " -Dmaven.test.redirectTestOutputToFile=true"
    " -Dsurefire.rerunFailingTestsCount=1"
)

_WARMUP_SELECT = """warmup="$(for m in {modules}; do
    find "$m/src/test/java" -name '*Test.java' ! -name 'Abstract*' -printf '%s %f\\n' 2>/dev/null | sort -n | head -n 1
done | cut -d' ' -f2 | sed 's/\\.java$//' | sort -u | paste -sd, -)"
test -n "$warmup\""""

_WARMUP_FLAGS = (
    '"-Dtest=$warmup" -Dsurefire.failIfNoSpecifiedTests=false'
    " -Dmaven.test.failure.ignore=true -Dsurefire.timeout=600"
)

_DISTRIBUTION_PREFIX = "tests/test-distribution/"

_RESULTS_MARKER = "##TEST_RESULTS##"

_SHELL_ENV = """\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export MAVEN_OPTS="-Xms1g -Xmx3g -Djava.awt.headless=true\""""

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

_ROUTING_FLAG = "_jetty_project_15857_to_14197_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org.lower() == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)


def _test_targets(test_patch: str) -> tuple[list[str], list[str], list[str]]:
    regular = set()
    scoped = set()
    classes = set()
    for path in re.findall(r"^diff --git a/(\S+) b/", test_patch, re.MULTILINE):
        if "/src/test/config/" in path:
            continue
        if "/src/" in path:
            module = path.split("/src/", 1)[0]
        elif path.endswith("/pom.xml"):
            module = path.rsplit("/", 1)[0]
        else:
            continue
        if not module.startswith(_DISTRIBUTION_PREFIX):
            regular.add(module)
        elif path.endswith(".java"):
            scoped.add(module)
            classes.add(path.rsplit("/", 1)[1].removesuffix(".java"))
    return sorted(regular), sorted(scoped), sorted(classes)


def _build_modules(test_patch: str) -> str:
    regular, scoped, _ = _test_targets(test_patch)
    return ",".join(sorted(regular + scoped))


def _test_commands(test_patch: str) -> str:
    regular, scoped, classes = _test_targets(test_patch)
    base = f"{_MVN} test --fail-at-end -Dmaven.test.failure.ignore=true {_SUREFIRE_FLAGS}"
    commands = []
    if regular:
        commands.append(f"{base} -pl {','.join(regular)} 2>&1 || status=$?")
    if scoped:
        commands.append(
            f"{base} -pl {','.join(scoped)} -Dtest={','.join(classes)} 2>&1 || status=$?"
        )
    return "\n{indent}".join(commands)


_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
{apply}{test_step}
results="$(find . -path '*/target/surefire-reports/TEST-*.xml' 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (maven exit code $status)"
    exit 1
fi
while read -r f; do
    echo "{marker} $f"
    cat "$f"
    echo
done <<< "$results"
exit "$status"
"""

_TEST_STEP = """status=0
{mvn} clean install -DskipTests --fail-at-end -pl {modules} -am 2>&1 || status=$?
if [ "$status" -eq 0 ]; then
    {test_commands}
fi"""

_TEST_STEP_EXCLUDING_UNCOMPILABLE = """log="$(mktemp)"
for attempt in 1 2 3 4 5; do
    status=0
    {mvn} clean install -DskipTests --fail-at-end -pl {modules} -am 2>&1 | tee "$log" || status=$?
    if [ "$status" -eq 0 ]; then
        {test_commands}
        break
    fi
    broken="$(grep -E '^\\[ERROR\\] /home/{repo}/.*/src/test/java/[^:]*\\.java:' "$log" \\
        | grep -oE '/home/{repo}/.*/src/test/java/[^:]*\\.java' \\
        | sort -u || true)"
    if [ -z "$broken" ]; then
        break
    fi
    while read -r f; do
        echo "Excluding uncompilable test source: ${{f#/home/{repo}/}}"
        rm -f "$f"
    done <<< "$broken"
done"""


class JettyProject15857To14197ImageBase(Image):
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
        return "maven:3.9.12-eclipse-temurin-21"

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


class JettyProject15857To14197ImageDefault(Image):
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
        return JettyProject15857To14197ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        modules = _build_modules(self.pr.test_patch)
        test_commands = _test_commands(self.pr.test_patch)
        test_step = _TEST_STEP.format(
            mvn=_MVN,
            modules=modules,
            test_commands=test_commands.format(indent="    "),
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
                """#!/bin/bash
set -e
{shell_env}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{mvn} clean install -DskipTests -pl {modules} -am
{warmup_select}
{mvn} test -pl {modules} {warmup_flags}

{mvn} -o test -pl {modules} {warmup_flags}
echo DEPS_OK
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    mvn=_MVN,
                    modules=modules,
                    warmup_select=_WARMUP_SELECT.format(modules=modules.replace(",", " ")),
                    warmup_flags=_WARMUP_FLAGS,
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply="",
                    test_step=test_step,
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
                    test_step=_TEST_STEP_EXCLUDING_UNCOMPILABLE.format(
                        mvn=_MVN,
                        modules=modules,
                        test_commands=test_commands.format(indent="        "),
                        repo=repo,
                    ),
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
                    test_step=test_step,
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
class JETTY_PROJECT_15857_TO_14197(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JettyProject15857To14197ImageDefault(self.pr, self._config)

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

        testcase_pattern = re.compile(
            r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL
        )
        name_pattern = re.compile(r'\bname="([^"]*)"')
        classname_pattern = re.compile(r'\bclassname="([^"]*)"')
        module_pattern = re.compile(r"\s*\./(\S+?)/target/surefire-reports/")

        statuses: dict[str, set[str]] = {}

        for chunk in clean.split(_RESULTS_MARKER)[1:]:
            module = module_pattern.match(chunk)
            if not module:
                continue
            for match in testcase_pattern.finditer(chunk):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                classname = classname_pattern.search(attrs)
                if not name or not classname:
                    continue

                top_level = classname.group(1).split("$", 1)[0].replace(".", "/")
                test_name = (
                    f"{module.group(1)}/src/test/java/{top_level}.java::{name.group(1)}"
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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
