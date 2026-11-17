import base64
import html
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "jenkinsci"
_REPO = "jenkins"
_INTERVAL = "jenkins_27406_to_26595"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_BASE_IMAGE = "maven:3.9.11-eclipse-temurin-21"

_MVN = "mvn -B -ntp -Dstyle.color=never"

_MVN_FLAGS = " ".join(
    [
        "-Dcheckstyle.skip=true",
        "-Dspotless.skip=true",
        "-Dspotless.check.skip=true",
        "-Dspotbugs.skip=true",
        "-Denforcer.skip=true",
        "-Daccess-modifier-checker.skip=true",
        "-Dban-junit4-imports.skip=true",
        "-Dyarn.lint.skip=true",
        "-Dinvoker.skip=true",
        "-Djacoco.skip=true",
        "-Dmaven.javadoc.skip=true",
        "-Dstrip.license.comments.skip=true",
    ]
)

_TEST_FLAGS = (
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false"
)

_VITEST_REPORT = "target/vitest-reports/TEST-vitest.xml"

_RESULTS_MARKER = "##TEST_RESULTS##"

_SHELL_ENV = f"""\
export CI=true
export JAVA_HOME=/opt/java/openjdk
export MAVEN_OPTS=-Xmx3g
export COREPACK_HOME=/root/.cache/node/corepack
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export PATH="/home/{_REPO}/node:$PATH"
if [ -s "${{SSL_CERT_FILE:-}}" ]; then
    export NODE_EXTRA_CA_CERTS="$SSL_CERT_FILE"
fi
p="${{https_proxy:-${{HTTPS_PROXY:-${{http_proxy:-${{HTTP_PROXY:-}}}}}}}}"
if [ -n "$p" ]; then
    export YARN_HTTP_PROXY="$p"
    export YARN_HTTPS_PROXY="$p"
    hp="${{p#*://}}"
    hp="${{hp##*@}}"
    hp="${{hp%%/*}}"
    case "$hp" in
        *:*) ph="${{hp%:*}}"; pp="${{hp##*:}}" ;;
        *) ph="$hp"; pp=80 ;;
    esac
    np="$(printf '%s' "${{no_proxy:-${{NO_PROXY:-}}}}" | tr ',' '|' | sed -e 's/^\\./*./' -e 's/|\\./|*./g')"
    export JAVA_TOOL_OPTIONS="${{JAVA_TOOL_OPTIONS:-}} -Dhttp.proxyHost=$ph -Dhttp.proxyPort=$pp -Dhttps.proxyHost=$ph -Dhttps.proxyPort=$pp -Dhttp.nonProxyHosts=$np"
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

_ROUTING_FLAG = "_jenkinsci_jenkins_27406_to_26595_routed"

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


def _scope(pr: PullRequest) -> tuple[list[str], list[str], bool]:
    modules: list[str] = []
    classes: list[str] = []
    has_js = False
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)\s*$", pr.test_patch, re.M):
        if path.startswith("src/test/js/") or re.match(r"^vitest\.config\.[cm]?[jt]s$", path):
            has_js = True
            continue
        m = re.match(r"^(.+?)/src/test/", path)
        if not m:
            continue
        if m.group(1) not in modules:
            modules.append(m.group(1))
        j = re.match(r"^.+?/src/test/java/(.+)\.java$", path)
        if j:
            name = j.group(1).replace("/", ".")
            if name not in classes:
                classes.append(name)
    return modules, classes, has_js


def _java_cmd(pr: PullRequest, goals: str, extra: str) -> str:
    modules, classes, _ = _scope(pr)
    if not modules:
        return ""
    selection = f" '-Dtest={','.join(classes)}'" if classes and "-DskipTests" not in extra else ""
    return f"{_MVN} {goals} -pl {','.join(modules)} -am{selection} {extra} {_MVN_FLAGS}"


def _mvn_test(pr: PullRequest) -> str:
    return _java_cmd(pr, "-fae clean package", _TEST_FLAGS)


def _mvn_gate(pr: PullRequest) -> str:
    return _java_cmd(pr, "-o clean package", "-DskipTests")


_RUN_TEMPLATE = """#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
{apply}find . \\( -path ./node -o -path ./.git -o -name node_modules \\) -prune -o -type d \\( -path '*/target/surefire-reports' -o -path '*/target/vitest-reports' \\) -prune -exec rm -rf {{}} +
{test_step}
results="$(find . \\( -path ./node -o -path ./.git -o -name node_modules \\) -prune -o -type f \\( -path '*/target/surefire-reports/TEST-*.xml' -o -path '*/target/vitest-reports/*.xml' \\) -print 2>/dev/null | sort || true)"
if [ -z "$results" ]; then
    echo "No test results found (test exit code $status)"
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

_JS_STEP = """js_status=0
corepack yarn install 2>&1 || js_status=$?
if [ "$js_status" -eq 0 ]; then
    corepack yarn vitest run --reporter=default --reporter=junit --outputFile.junit={report} 2>&1 || js_status=$?
fi
if [ "$status" -eq 0 ]; then
    status="$js_status"
fi"""


def _test_step(pr: PullRequest, exclude_uncompilable: bool) -> str:
    _, _, has_js = _scope(pr)
    mvn_test = _mvn_test(pr)
    if not mvn_test:
        step = "status=0"
    elif exclude_uncompilable:
        step = _TEST_STEP_EXCLUDING_UNCOMPILABLE.format(mvn_test=mvn_test, repo=pr.repo)
    else:
        step = _TEST_STEP.format(mvn_test=mvn_test)
    if has_js:
        step += "\n" + _JS_STEP.format(report=_VITEST_REPORT)
    return step


def _provision(pr: PullRequest) -> str:
    repo = pr.repo
    lines = [f"{_MVN} -N initialize {_MVN_FLAGS}"]
    mvn_test = _mvn_test(pr)
    if mvn_test:
        lines.append(f"{mvn_test} || true")
    lines.append("")
    lines.append(f"test -x /home/{repo}/node/node")
    lines.append(f"test -e /home/{repo}/node/corepack")
    lines.append("node --version")
    lines.append("corepack yarn --version")
    lines.append("corepack yarn install")
    lines.append(f"test -d /home/{repo}/node_modules")
    if mvn_test:
        lines.append(_mvn_gate(pr))
        lines.append(f'test -n "$(ls -A /home/{repo}/war/src/main/webapp/jsbundles)"')
    return "\n".join(lines)


class Jenkins27406To26595ImageBase(Image):
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

RUN git config --global --add safe.directory '*'

RUN echo "{_JVM_PROXY_B64}" | base64 -d | sh

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Jenkins27406To26595ImageDefault(Image):
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
        return Jenkins27406To26595ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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

{provision}
test "$(git rev-parse HEAD)" = "{sha}"
echo DEPS_OK
""".format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    provision=_provision(self.pr),
                ),
            ),
            File(
                ".",
                "run.sh",
                _RUN_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    apply="",
                    test_step=_test_step(self.pr, False),
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
                    test_step=_test_step(self.pr, True),
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
                    test_step=_test_step(self.pr, False),
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
@Instance.register(_ORG, _REPO)
class JENKINS_27406_TO_26595(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Jenkins27406To26595ImageDefault(self.pr, self._config)

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
        clean = clean.replace("\r", "")
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
            is_vitest = "/vitest-reports/" in f"/{rel}"
            prefix = f"{rel.split('/target/', 1)[0]}/" if "/target/" in rel else ""
            for match in testcase_pattern.finditer(content):
                attrs = match.group(1)
                name = name_pattern.search(attrs)
                classname = classname_pattern.search(attrs)
                if not name or not name.group(1):
                    continue
                test_case = html.unescape(name.group(1)).strip()
                owner = html.unescape(classname.group(1)).strip() if classname else ""

                if is_vitest:
                    test_name = f"{owner or 'vitest'}::{test_case}"
                else:
                    if not owner:
                        continue
                    top_level, _, nested = owner.partition("$")
                    scope = "".join(f"{part}::" for part in nested.split("$") if part)
                    test_name = (
                        f"{prefix}src/test/java/{top_level.replace('.', '/')}.java"
                        f"::{scope}{test_case}"
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
