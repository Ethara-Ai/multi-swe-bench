import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.apache.druid_0_to_16976 import (
    _build_pl_flag,
    _extract_test_classes_from_patch,
    _patch_paths,
)

_MAVEN_IMAGE = "maven:3.9.11-eclipse-temurin-25"
_WARM_ATTEMPTS = 3
_EXCLUDED_MODULES = frozenset({"embedded-tests"})
def _graded_classes(pr: PullRequest) -> set:
    classes = set(
        _extract_test_classes_from_patch(pr.test_patch, _EXCLUDED_MODULES)
    )
    for path in _patch_paths(pr.fix_patch):
        if not path.endswith(".java"):
            continue
        if path.split("/", 1)[0] in _EXCLUDED_MODULES:
            continue
        stem = path.rsplit("/", 1)[-1][: -len(".java")]
        classes.add(stem if "/src/test/" in path else stem + "Test")
    return classes


def _pkg_dirs(pr: PullRequest) -> str:
    dirs = set()
    for path in _patch_paths(pr.test_patch) + _patch_paths(pr.fix_patch):
        if not path.endswith(".java"):
            continue
        if path.split("/", 1)[0] in _EXCLUDED_MODULES:
            continue
        parent = path.rsplit("/", 1)[0]
        dirs.add(parent.replace("/src/main/java/", "/src/test/java/"))
    return " ".join(sorted(dirs))


def _module_paths(pl_flag: str) -> str:
    if not pl_flag:
        return "."
    parts = pl_flag.split()
    if "-pl" not in parts:
        return "."
    return parts[parts.index("-pl") + 1].replace(",", " ")


_TRANSFER_ERROR_RE = (
    "Could not transfer artifact|Could not resolve dependencies|"
    "Connection timed out|Connection reset|UnknownHostException|"
    "failed to transfer from"
)
_NET_FLAGS = (
    "-Dmaven.wagon.http.retryHandler.count=5 "
    "-Dmaven.wagon.http.retryHandler.requestSentEnabled=true "
    "-Dmaven.wagon.httpconnectionManager.ttlSeconds=120 "
    "-Dmaven.wagon.rto=60000"
)
_BASE_IMAGE = "eclipse-temurin:25-jdk"
_JDK_HOME = "/opt/java/openjdk"


class DruidJdk25ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_ERA_MIN}_to_{_ERA_MAX}"

    def workdir(self) -> str:
        return f"base-{_ERA_MIN}_to_{_ERA_MAX}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CI=true \\
    JAVA_HOME={_JDK_HOME} \\
    MAVEN_OPTS="-Xmx2g -Djava.net.preferIPv4Stack=true" \\
    PATH=/opt/maven/bin:{_JDK_HOME}/bin:${{PATH}} \\
    TEST_ENV_PROVIDER=oss-source \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    gnupg \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_MAVEN_IMAGE} /usr/share/maven /opt/maven

RUN /opt/maven/bin/mvn --version

RUN git config --global --add safe.directory '*'

{code}

WORKDIR /home/{self.pr.repo}
RUN git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class DruidJdk25ImageDefault(Image):
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
        return DruidJdk25ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pl_flag = _build_pl_flag(self.pr, _EXCLUDED_MODULES)
        graded = sorted(_graded_classes(self.pr))
        specific_csv = ",".join(f"{c}*" for c in graded)
        specific_list = " ".join(graded)
        pkg_dirs = _pkg_dirs(self.pr)
        test_flag = f"-Dtest={specific_csv}" if specific_csv else ""
        suffix = " ".join(x for x in (pl_flag, test_flag) if x)
        mvn_prepare_base = (
            "mvn clean test -fn -B " + _NET_FLAGS + " -Dsurefire.useFile=false "
            "-Dmaven.test.skip=false -DfailIfNoTests=false "
            "-Dsurefire.failIfNoSpecifiedTests=false"
        )
        mvn_prepare_cmd = f"{mvn_prepare_base} {suffix}" if suffix else mvn_prepare_base
        mvn_run_base = (
            "mvn test -fn -B " + _NET_FLAGS + " -Dsurefire.useFile=false "
            "-Dmaven.test.skip=false -DfailIfNoTests=false "
            "-Dsurefire.failIfNoSpecifiedTests=false"
        )
        mvn_run_cmd = f"{mvn_run_base} {pl_flag}" if pl_flag else mvn_run_base
        mvn_gate_base = "mvn test-compile -o -B -fn"
        go_offline_base = "mvn dependency:go-offline -B " + _NET_FLAGS
        fallback_base = (
            "mvn test -B -fn " + _NET_FLAGS + " -Dsurefire.useFile=false "
            "-DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false"
        )
        fallback_cmd = (
            f"{fallback_base} {pl_flag}" if pl_flag else fallback_base
        )
        module_paths = _module_paths(pl_flag)
        go_offline_cmd = (
            f"{go_offline_base} {pl_flag}" if pl_flag else go_offline_base
        )
        mvn_gate_cmd = f"{mvn_gate_base} {pl_flag}" if pl_flag else mvn_gate_base
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

GRADED_TESTS="{specific_csv}"
RUNNABLE_AT_BASE=0
for c in {specific_list}; do
    for f in $(find {module_paths} -path '*/src/test/java/*' -name "$c.java" 2>/dev/null); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            RUNNABLE_AT_BASE=1
            break
        fi
    done
    if [ "$RUNNABLE_AT_BASE" -eq 1 ]; then
        break
    fi
done
echo "GRADED_SPECIFIC_RUNNABLE_AT_BASE=$RUNNABLE_AT_BASE"
if [ "$RUNNABLE_AT_BASE" -eq 0 ]; then
    EXPAND_DIRS="{pkg_dirs}"
    if [ -z "$EXPAND_DIRS" ]; then
        EXPAND_DIRS="{module_paths}"
    fi
    for f in $(find $EXPAND_DIRS -path '*/src/test/java/*' -name '*Test.java' 2>/dev/null); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            GRADED_TESTS="$GRADED_TESTS,$(basename "$f" .java)"
        fi
    done
fi
echo "$GRADED_TESTS" | sed 's/^,//;s/,$//' > /home/graded_tests.txt
echo "GRADED_TESTS_COUNT=$(awk -F, '{{print NF}}' /home/graded_tests.txt)"

find . -path '*/target/surefire-reports/TEST-*.xml' -delete

WARM_REPORTS=0
WARM_ATTEMPT=0
until [ "$WARM_ATTEMPT" -ge {warm_attempts} ]; do
    WARM_ATTEMPT=$((WARM_ATTEMPT + 1))
    if [ "$WARM_ATTEMPT" -gt 1 ]; then
        find /root/.m2 -name '*.lastUpdated' -delete 2>/dev/null || true
        {mvn_cmd} -U > /tmp/warm-mvn.log 2>&1 || true
    else
        {mvn_cmd} > /tmp/warm-mvn.log 2>&1 || true
    fi
    grep -E '^\\[INFO\\] BUILD|SUCCESS \\[|FAILURE \\[|^\\[ERROR\\]' /tmp/warm-mvn.log | tail -60
    WARM_REPORTS="$(find . -path '*/target/surefire-reports/TEST-*.xml' | wc -l | tr -d ' ')"
    echo "WARM_ATTEMPT=$WARM_ATTEMPT WARM_REPORTS=$WARM_REPORTS"
    if ! grep -qE '{transfer_re}' /tmp/warm-mvn.log; then
        break
    fi
    sleep 15
done

echo "WARM_JUNIT_REPORTS=$WARM_REPORTS"

{go_offline_cmd} > /tmp/go-offline.log 2>&1 || true

SUREFIRE_PROVIDERS="$(find /root/.m2 -name 'surefire-junit*.jar' -o -name 'surefire-testng*.jar' | wc -l | tr -d ' ')"
echo "SUREFIRE_PROVIDERS=$SUREFIRE_PROVIDERS"

if [ "$SUREFIRE_PROVIDERS" -eq 0 ]; then
    FALLBACK_CLASS=""
    for f in $(find {module_paths} -path '*/src/test/java/*' -name '*Test.java' 2>/dev/null | head -60); do
        if grep -q '@Test' "$f" && ! grep -qE '^[[:space:]]*(public[[:space:]]+)?abstract[[:space:]]+class' "$f"; then
            FALLBACK_CLASS="$(basename "$f" .java)"
            break
        fi
    done
    echo "SUREFIRE_FALLBACK_CLASS=$FALLBACK_CLASS"
    if [ -n "$FALLBACK_CLASS" ]; then
        {fallback_cmd} -Dtest="$FALLBACK_CLASS" > /tmp/fallback-mvn.log 2>&1 || true
        grep -E '^\\[INFO\\] BUILD|Tests run:' /tmp/fallback-mvn.log | tail -5
        SUREFIRE_PROVIDERS="$(find /root/.m2 -name 'surefire-junit*.jar' -o -name 'surefire-testng*.jar' | wc -l | tr -d ' ')"
        echo "SUREFIRE_PROVIDERS_AFTER_FALLBACK=$SUREFIRE_PROVIDERS"
    fi
fi

set +e
{gate_cmd} > /tmp/gate-mvn.log 2>&1
GATE_STATUS=$?
set -e
echo "GATE_MVN_EXIT=$GATE_STATUS"

if [ "$GATE_STATUS" -ne 0 ]; then
    tail -120 /tmp/gate-mvn.log
fi

! grep -qE 'Could not resolve dependencies|Could not transfer artifact|Cannot access .* in offline mode|Non-resolvable parent POM' /tmp/gate-mvn.log
test "$GATE_STATUS" -eq 0
test "$SUREFIRE_PROVIDERS" -gt 0
echo "DEPS_OK"
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    mvn_cmd=mvn_prepare_cmd,
                    gate_cmd=mvn_gate_cmd,
                    warm_attempts=_WARM_ATTEMPTS,
                    go_offline_cmd=go_offline_cmd,
                    transfer_re=_TRANSFER_ERROR_RE,
                    fallback_cmd=fallback_cmd,
                    module_paths=module_paths,
                    specific_csv=specific_csv,
                    specific_list=specific_list,
                    pkg_dirs=pkg_dirs,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
find . -path '*/target/surefire-reports/TEST-*.xml' -delete
TEST_FILTER="$(cat /home/graded_tests.txt 2>/dev/null || true)"
if [ -n "$TEST_FILTER" ]; then
    {mvn_cmd} -Dtest="$TEST_FILTER" || true
else
    {mvn_cmd} || true
fi

test -n "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)"
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} +
""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
find . -path '*/target/surefire-reports/TEST-*.xml' -delete
TEST_FILTER="$(cat /home/graded_tests.txt 2>/dev/null || true)"
if [ -n "$TEST_FILTER" ]; then
    {mvn_cmd} -Dtest="$TEST_FILTER" || true
else
    {mvn_cmd} || true
fi

test -n "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)"
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} +

""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
find . -path '*/target/surefire-reports/TEST-*.xml' -delete
TEST_FILTER="$(cat /home/graded_tests.txt 2>/dev/null || true)"
if [ -n "$TEST_FILTER" ]; then
    {mvn_cmd} -Dtest="$TEST_FILTER" || true
else
    {mvn_cmd} || true
fi

test -n "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)"
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} +

""".format(repo=self.pr.repo, mvn_cmd=mvn_run_cmd),
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{proxy_setup}

{copy_commands}

{prepare_commands}

RUN set -eux; \\
    git checkout --detach "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{self.pr.base.sha}")"; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("apache", "druid_18790_to_99999")
class DruidJdk25(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DruidJdk25ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for m in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(m.group(1)))
            cls = attrs.get("classname", "")
            meth = attrs.get("name", "")
            if not meth:
                continue
            saw_xml = True
            name = f"{cls}.{meth}" if cls else meth
            body = m.group(3) or ""
            if "<failure" in body or "<error" in body:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif "<skipped" in body:
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        if saw_xml:
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )


        re_pass_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
            )
        ]
        re_fail_tests = [
            re.compile(
                r"Running\s+(.+?)\s*\n(?:(?!.*Tests run:)(?!.*Running\s)(?!.*Results:).*\n)*.*?Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*<<<\s*FAILURE!"
            )
        ]

        for re_fail_test in re_fail_tests:
            for m in re_fail_test.finditer(test_log):
                failed_tests.add(m.group(1))

        for re_pass_test in re_pass_tests:
            for m in re_pass_test.finditer(test_log):
                test_name = m.group(1)
                if test_name in failed_tests:
                    continue
                tests_run = int(m.group(2))
                failures = int(m.group(3))
                errors = int(m.group(4))
                skipped = int(m.group(5))
                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


def _era_bounds(module_name: str) -> tuple[int, int]:
    parts = module_name.rsplit(".", 1)[-1].split("_")
    numbers = [int(p) for p in parts if p.isdigit()]
    return numbers[0], numbers[1]


_ERA_MIN, _ERA_MAX = _era_bounds(__name__)

_INCUMBENT = Instance._registry.get("apache/druid")


@Instance.register("apache", "druid")
class DruidEraDispatch18790To99999(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _ERA_MIN <= pr.number <= _ERA_MAX:
            return DruidJdk25(pr, config, *args, **kwargs)
        if _INCUMBENT is not None:
            return _INCUMBENT(pr, config, *args, **kwargs)
        raise ValueError(
            f"apache/druid#{pr.number} is outside {_ERA_MIN}-{_ERA_MAX} and no "
            f"other apache/druid adapter owns the bare name"
        )
