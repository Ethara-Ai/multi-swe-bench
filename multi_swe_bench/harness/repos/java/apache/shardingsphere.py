import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "apache"
_REPO = "shardingsphere"

_BASE_TAG = "base"

_BASE_IMAGE = "eclipse-temurin:21-jdk-noble"
_MAVEN_VERSION = "3.9.16"
_MAVEN_SHA512 = (
    "831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0caf"
    "b8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6"
)
_BASE_APT = "git ca-certificates curl"

_SUREFIRE_SENTINEL = "ShardingSphereNoSuchSurefireTest"
_FAILSAFE_SENTINEL = "ShardingSphereNoSuchFailsafeIT"

_MVN_FLAGS = (
    "-B -ntp -fae "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dit.failIfNoSpecifiedTests=false "
    "-Dfailsafe.failIfNoSpecifiedTests=false "
    "-Djacoco.skip=true "
    "-Denforcer.skip=true "
    "-Drat.skip=true "
    "-Dcheckstyle.skip=true "
    "-Dspotless.skip=true "
    "-Dspotless.check.skip=true "
    "-Dspotbugs.skip=true "
    "-Dpmd.skip=true "
    "-Dcpd.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dmaven.source.skip=true "
    "-Dlicense.skip=true "
    "-Dmaven.install.skip=true "
    "-Dmaven.deploy.skip=true "
    "-Dmaven.wagon.http.retryHandler.count=10 "
    "-Dmaven.wagon.http.retryHandler.requestSentEnabled=true "
    "-Daether.connector.http.retryHandler.count=10 "
    "-Daether.connector.http.retryHandler.interval=10000 "
    "-Daether.connector.http.retryHandler.intervalMax=60000 "
    "-Daether.connector.http.retryHandler.serviceUnavailable=429,502,503,504 "
    "-Daether.connector.basic.threads=2"
)

_MVN_TIMEOUT = "timeout -k 60 7200"
_MVN_TIMEOUT_PREPARE = "timeout -k 60 10800"

_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_APPLY_FN = r"""_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}"""

_DERIVE_TARGETS = r"""_module_dir() {
    local _d
    case "$1" in
        */*) _d="${1%/*}" ;;
        *) return 1 ;;
    esac
    while [ -n "$_d" ] && [ "$_d" != "." ] && [ "$_d" != "/" ]; do
        if [ -f "$_d/pom.xml" ]; then
            printf '%s' "$_d"
            return 0
        fi
        case "$_d" in
            */*) _d="${_d%/*}" ;;
            *) _d="" ;;
        esac
    done
    return 1
}

_is_test_class() {
    case "$1" in
        Test*|*Test|*Tests|*TestCase|*IT|*ITCase) return 0 ;;
    esac
    return 1
}

_PATHS="$(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/test.patch | sort -u)"

_MODULES=""
for _p in $_PATHS; do
    case "$_p" in
        test/e2e/*) continue ;;
    esac
    _m="$(_module_dir "$_p")" || continue
    _MODULES="$_MODULES $_m"
done
_MODULES="$(printf '%s\n' $_MODULES | sed '/^$/d' | sort -u)"

_SEL_LIST=""
_SF_LIST=""
_FS_LIST=""
for _m in $_MODULES; do
    _cls=""
    for _p in $_PATHS; do
        case "$_p" in
            "$_m"/src/test/java/*.java) ;;
            *) continue ;;
        esac
        _c="${_p##*/}"
        _c="${_c%.java}"
        _is_test_class "$_c" || continue
        _cls="$_cls $_c"
    done
    if [ -z "$_cls" ] && [ -d "$_m/src/test/java" ]; then
        for _f in $(find "$_m/src/test/java" -type f -name '*.java' | sort); do
            _c="${_f##*/}"
            _c="${_c%.java}"
            _is_test_class "$_c" || continue
            _cls="$_cls $_c"
        done
    fi
    [ -n "$_cls" ] || continue
    _SEL_LIST="$_SEL_LIST $_m"
    for _c in $_cls; do
        case "$_c" in
            *IT|*ITCase) _FS_LIST="$_FS_LIST $_c" ;;
            *) _SF_LIST="$_SF_LIST $_c" ;;
        esac
    done
done

SEL="$(printf '%s\n' $_SEL_LIST | sed '/^$/d' | sort -u | paste -sd, -)"
SF_CLASSES="$(printf '%s\n' $_SF_LIST | sed '/^$/d' | sort -u | paste -sd, -)"
FS_CLASSES="$(printf '%s\n' $_FS_LIST | sed '/^$/d' | sort -u | paste -sd, -)"
SF_SELECTED="${SF_CLASSES:-@@SUREFIRE_SENTINEL@@}"
FS_SELECTED="${FS_CLASSES:-@@FAILSAFE_SENTINEL@@}"
"""

_REPAIR_FN = r"""_repair_test_sources() {
    local _log _dropped _f _i
    _i=1
    while [ "$_i" -le 5 ]; do
        _log="$(mktemp)"
        if "$@" >"$_log" 2>&1; then
            cat "$_log"
            rm -f "$_log"
            return 0
        fi
        cat "$_log"
        _dropped=0
        for _f in $(sed -nE 's#^\[ERROR\] (@@REPO_DIR@@[^:]*/src/test/java/[^:]*\.java):\[[0-9]+,[0-9]+\].*$#\1#p' "$_log" | sort -u); do
            if [ -f "$_f" ]; then
                echo "repair: dropping uncompilable test source $_f"
                rm -f "$_f"
                _dropped=$((_dropped + 1))
            fi
        done
        rm -f "$_log"
        if [ "$_dropped" -eq 0 ]; then return 1; fi
        _i=$((_i + 1))
    done
    return 1
}
"""

_RUN_TARGETS = r"""if [ -z "$SEL" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi

echo "SELECTED_MODULES=$SEL"
echo "SELECTED_SUREFIRE=$SF_SELECTED"
echo "SELECTED_FAILSAFE=$FS_SELECTED"

find . -type d -name surefire-reports -prune -exec rm -rf {} +
find . -type d -name failsafe-reports -prune -exec rm -rf {} +

@@REPAIR_FN@@

_repair_test_sources @@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ test-compile -pl "$SEL" -am || true

MVN_RC=0
@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ verify -pl "$SEL" -am -Dtest="$SF_SELECTED" -Dit.test="$FS_SELECTED" || MVN_RC=$?

find . -path '*/surefire-reports/TEST-*.xml' -exec cat {} +
find . -path '*/failsafe-reports/TEST-*.xml' -exec cat {} +

if [ "$MVN_RC" -ne 0 ]; then
    echo "MVN_EXIT_CODE=$MVN_RC"
    exit "$MVN_RC"
fi
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "@@SHA@@"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "@@SHA@@"

@@DERIVE_TARGETS@@

echo "prepare.sh: modules=[$SEL]"

_mvn_retry() {
    local _max="$1"
    shift
    local _i=1
    while :; do
        if "$@"; then return 0; fi
        if [ "$_i" -ge "$_max" ]; then return 1; fi
        echo "prepare.sh: mvn attempt $_i failed, backing off $((_i * 45))s"
        sleep $((_i * 45))
        _i=$((_i + 1))
    done
}

@@REPAIR_FN@@

_mvn_retry 6 @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ -N validate || true

if [ -n "$SEL" ]; then
    _mvn_retry 3 @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ verify -pl "$SEL" -am -Dtest="$SF_SELECTED" -Dit.test="$FS_SELECTED" || true
fi

git clean -fdx
find . -type d -name target -prune -exec rm -rf {} +

if [ -n "$SEL" ]; then
    _repair_test_sources @@MVN_TIMEOUT_PREPARE@@ mvn -o @@MVN_FLAGS@@ verify -pl "$SEL" -am -Dtest=@@SUREFIRE_SENTINEL@@ -Dit.test=@@FAILSAFE_SENTINEL@@
else
    @@MVN_TIMEOUT_PREPARE@@ mvn -o @@MVN_FLAGS@@ -N validate
fi

find . -type d -name target -prune -exec rm -rf {} +
git reset --hard
git clean -fdx
test "$(git rev-parse HEAD)" = "@@SHA@@"
echo "DEPS_OK"
"""

_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_TEST_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@

_apply_patches /home/test.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_FIX_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@

_apply_patches /home/test.patch /home/fix.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM @@IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}
@@GLOBAL_ENV@@
LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN mkdir -p /usr/share/binfmts && \
    apt-get update && \
    apt-get install -y --no-install-recommends @@APT@@ && \
    rm -rf /var/lib/apt/lists/*

ENV MAVEN_HOME=/opt/apache-maven-@@MAVEN_VERSION@@ \
    MAVEN_OPTS="-Xmx3g"

RUN set -eux; \
    curl -fsSL --retry 5 --retry-delay 5 -o /tmp/maven.tar.gz "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/@@MAVEN_VERSION@@/apache-maven-@@MAVEN_VERSION@@-bin.tar.gz"; \
    echo "@@MAVEN_SHA512@@  /tmp/maven.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/maven.tar.gz -C /opt; \
    ln -s /opt/apache-maven-@@MAVEN_VERSION@@/bin/mvn /usr/local/bin/mvn; \
    rm -f /tmp/maven.tar.gz; \
    java -version; \
    mvn -v

RUN git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@

WORKDIR /home/@@REPO@@
@@CLEAR_ENV@@
CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM @@BASE_IMAGE@@

ARG BASE_COMMIT="@@SHA@@"
@@GLOBAL_ENV@@
@@COPY@@

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
@@CLEAR_ENV@@
"""


def _render(body: str, values: dict) -> str:
    for key, value in values.items():
        body = body.replace("@@%s@@" % key, value)
    return body


def _optional_block(block: str) -> str:
    return "\n%s\n" % block if block else ""


class ShardingSphereImageBase(Image):
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
        if self.config.need_clone:
            code = (
                'RUN git clone "${REPO_URL}" /home/%s && \\\n    cd /home/%s && git rev-parse HEAD >/dev/null'
                % (
                    self.pr.repo,
                    self.pr.repo,
                )
            )
        else:
            code = "COPY %s /home/%s" % (self.pr.repo, self.pr.repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": str(self.dependency()),
                "ORG": self.pr.org,
                "REPO": self.pr.repo,
                "APT": _BASE_APT,
                "MAVEN_VERSION": _MAVEN_VERSION,
                "MAVEN_SHA512": _MAVEN_SHA512,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class ShardingSphereImageDefault(Image):
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
        return ShardingSphereImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "MVN_FLAGS": _MVN_FLAGS,
            "MVN_TIMEOUT": _MVN_TIMEOUT,
            "MVN_TIMEOUT_PREPARE": _MVN_TIMEOUT_PREPARE,
            "SUREFIRE_SENTINEL": _SUREFIRE_SENTINEL,
            "FAILSAFE_SENTINEL": _FAILSAFE_SENTINEL,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "APPLY_FN": _APPLY_FN,
                "DERIVE_TARGETS": _DERIVE_TARGETS,
                "RUN_TARGETS": _RUN_TARGETS,
                "REPAIR_FN": _REPAIR_FN,
            },
        )
        return _render(body, self._values())

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._script(_PREPARE)),
            File(".", "run.sh", self._script(_RUN)),
            File(".", "test-run.sh", self._script(_TEST_RUN)),
            File(".", "fix-run.sh", self._script(_FIX_RUN)),
        ]

    def dockerfile(self) -> str:
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        values = self._values()
        values.update(
            {
                "BASE_IMAGE": self.dependency().image_full_name(),
                "COPY": copy_commands,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            }
        )
        return _render(_PR_DOCKERFILE, values)


def parse_surefire_xml(test_log: str) -> TestResult:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
    name_re = re.compile(r'\bname="([^"]*)"')
    classname_re = re.compile(r'\bclassname="([^"]*)"')

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for match in testcase_re.finditer(clean):
        attrs = match.group(1)
        name_match = name_re.search(attrs)
        if not name_match:
            continue
        classname_match = classname_re.search(attrs)
        name = name_match.group(1)
        classname = classname_match.group(1) if classname_match else ""
        test_id = "%s#%s" % (classname, name) if classname else name

        if match.group(2) == "/>":
            passed_tests.add(test_id)
            continue

        inner = match.group(3) or ""
        if "<failure" in inner or "<error" in inner:
            failed_tests.add(test_id)
        elif "<skipped" in inner:
            skipped_tests.add(test_id)
        else:
            passed_tests.add(test_id)

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


@Instance.register(_ORG, _REPO)
class SHARDINGSPHERE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ShardingSphereImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_xml(test_log)
