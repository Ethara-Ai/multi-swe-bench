import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "feign_3608_to_3608"
_BASE_TAG = f"base-{_INTERVAL}"
_BASE_IMAGE = "maven:3.9.11-eclipse-temurin-25"
_MAVEN_OPTS = "-Xmx1536m -XX:MaxMetaspaceSize=512m"

_COMMON_FLAGS = (
    "-Denforcer.skip=true -Dcheckstyle.skip=true -Drat.skip=true -Dspotless.check.skip=true "
    "-Dspotless.apply.skip=true -Dlicense.skip=true -Dformatter.skip=true -Dimpsort.skip=true "
    "-Dmaven.javadoc.skip=true -Djacoco.skip=true -Dmaven.wagon.http.retryHandler.count=5"
)
_INSTALL_FLAGS = "-DskipTests -Dtoolchain.skip=true -Dmaven.compiler.proc=full -Djapicmp.skip=true -Danimal.sniffer.skip=true -Dgcf.skip=true -Dsort.skip=true " + _COMMON_FLAGS
_TEST_FLAGS = (
    "-Dmaven.test.failure.ignore=true -DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false "
    "-Dtoolchain.skip=true -Dmaven.compiler.proc=full -Djapicmp.skip=true -Danimal.sniffer.skip=true -Dgcf.skip=true -Dsort.skip=true " + _COMMON_FLAGS
)

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6
FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

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
    MAVEN_OPTS="__MAVEN_OPTS__" \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image (JDK 25, Maven 3.9)" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    USE_SYSTEM_CA_CERTS=1 /__cacert_entrypoint.sh true >/dev/null

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__BASE_SHA__"

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/__REPO__; \
    git checkout --detach "${BASE_COMMIT}"; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git remote)"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        cd /home/__REPO__ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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
"""

_ENV_SH = r"""export CI=true
export MAVEN_OPTS="__MAVEN_OPTS__"
"""

_TARGETS_SH = r"""collect_targets() {
    local p m r rel parent
    : > /tmp/msb_mods.txt
    for p in $(awk '/^diff --git a\//{p=$3; sub(/^a\//,"",p); print p}' /home/test.patch /home/fix.patch | sort -u); do
        case "$p" in
            src/test/java/*|*/src/test/java/*)
                case "${p##*/}" in
                    Test*.java|*Test.java|*Tests.java|*TestCase.java) ;;
                    *) continue ;;
                esac ;;
            src/main/*|*/src/main/*) ;;
            *) continue ;;
        esac
        case "$p" in
            src/*) m="." ;;
            *) m="${p%%/src/*}" ;;
        esac
        [ -f "$m/pom.xml" ] && echo "$m" >> /tmp/msb_mods.txt
    done
    : > /tmp/msb_targets.txt
    for m in $(sort -u /tmp/msb_mods.txt); do
        r="$m"
        while [ "$r" != "." ]; do
            parent="$(dirname "$r")"
            grep -q "<module>$(basename "$r")</module>" "$parent/pom.xml" 2>/dev/null || break
            r="$parent"
        done
        if [ "$r" = "$m" ]; then
            rel="."
        elif [ "$r" = "." ]; then
            rel="$m"
        else
            rel="${m#"$r"/}"
        fi
        echo "$r $rel" >> /tmp/msb_targets.txt
    done
    sort -u /tmp/msb_targets.txt | awk '{a[$1]=a[$1] (a[$1]?",":"") $2} END{for(k in a) print k, a[k]}' | sort > /tmp/msb_groups.txt
    cat /tmp/msb_groups.txt
}

msb_mvn() {
    local root="$1" mods="$2"
    shift 2
    local pl=()
    case ",$mods," in
        *,.,*) ;;
        *) pl=(-pl "$mods") ;;
    esac
    (cd "$root" && timeout 10800 mvn -B -ntp "${pl[@]}" "$@")
}
"""

_SETTINGS_SH = r"""mkdir -p /root/.m2
{
    echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">'
    proxy_url="${https_proxy:-${http_proxy:-}}"
    if [ -n "$proxy_url" ]; then
        hp="${proxy_url#*://}"
        hp="${hp%%/*}"
        hp="${hp##*@}"
        host="${hp%%:*}"
        port="${hp##*:}"
        [ "$port" = "$hp" ] && port=80
        nph="$(printf '%s' "${no_proxy:-localhost,127.0.0.1}" | tr ',' '|')"
        echo "  <proxies><proxy><id>build-proxy</id><active>true</active><protocol>http</protocol><host>${host}</host><port>${port}</port><nonProxyHosts>${nph}</nonProxyHosts></proxy></proxies>"
    fi
    echo '</settings>'
} > /root/.m2/settings.xml
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__BASE_SHA__"
fi
git checkout --detach "__BASE_SHA__"
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

__ENV__
__SETTINGS__
__TARGETS__
collect_targets
test -s /tmp/msb_groups.txt

while read -r root mods; do
    attempt=1
    until msb_mvn "$root" "$mods" install -am __INSTALL_FLAGS__; do
        test "$attempt" -lt 3
        attempt=$((attempt + 1))
        sleep 30
    done
    msb_mvn "$root" "$mods" test __TEST_FLAGS__ > /tmp/warm-test.log 2>&1 || true
done < /tmp/msb_groups.txt

git reset --hard
git clean -fdx

gate=0
probe=""
for f in $(awk '/^diff --git a\//{p=$3; sub(/^a\//,"",p); print p}' /home/test.patch | grep -E '/src/test/java/.*(Test|Tests|TestCase)\.java$' || true); do
    if [ -f "$f" ]; then
        probe="$(basename "$f" .java)"
        break
    fi
done
while read -r root mods; do
    msb_mvn "$root" "$mods" -o test-compile __TEST_FLAGS__ || gate=1
    if [ -n "$probe" ]; then
        msb_mvn "$root" "$mods" -o test "-Dtest=${probe}" __TEST_FLAGS__ > /tmp/gate-test.log 2>&1 || gate=1
        grep -qE 'Could not resolve|offline mode|Could not transfer' /tmp/gate-test.log && gate=1
    fi
done < /tmp/msb_groups.txt
if [ -n "$probe" ] && [ -z "$(find . -path '*/target/surefire-reports/TEST-*.xml' -print -quit)" ]; then
    gate=1
fi
git reset --hard && git clean -fdx
printf '%s\n' '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"/>' > /root/.m2/settings.xml
test "$gate" -eq 0
echo "DEPS_OK"
"""

_RUN_BODY_SH = r"""
__TARGETS__
collect_targets
find . -type d -name surefire-reports -prune -exec rm -rf {} +

status=0
while read -r root mods; do
    msb_mvn "$root" "$mods" -o test __TEST_FLAGS__ || status=1
done < /tmp/msb_groups.txt

echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' | sort | while read -r f; do
    cat "$f"
    echo
done
echo "===== SUREFIRE REPORTS END ====="
exit "$status"
"""

_STAGE_SH = r"""#!/bin/bash
set -eo pipefail
__ENV__
cd /home/__REPO__
__APPLY__
__RUN_BODY__"""

_STAGES = {
    "run.sh": "",
    "test-run.sh": "git apply --whitespace=nowarn /home/test.patch",
    "fix-run.sh": "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_surefire_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")
    for match in _CASE_RE.finditer(clean_log):
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        method = attrs.get("name", "")
        classname = attrs.get("classname", "")
        if not method or not classname:
            continue
        name = f"{classname}#{method}"
        body = re.sub(r"<(system-out|system-err)\b.*?</\1>|<!\[CDATA\[.*?\]\]>", "", match.group(3) or "", flags=re.S)
        if re.search(r"<(failure|error)\b", body):
            failed_tests.add(name)
        elif "<skipped" in body:
            skipped_tests.add(name)
        else:
            passed_tests.add(name)
    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class Feign_3608_TO_3608ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", _BASE_IMAGE)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class Feign_3608_TO_3608ImageDefault(Image):
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
        return Feign_3608_TO_3608ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__RUN_BODY__", _RUN_BODY_SH)
            .replace("__ENV__", _ENV_SH)
            .replace("__SETTINGS__", _SETTINGS_SH)
            .replace("__TARGETS__", _TARGETS_SH)
            .replace("__INSTALL_FLAGS__", _INSTALL_FLAGS)
            .replace("__TEST_FLAGS__", _TEST_FLAGS)
            .replace("__MAVEN_OPTS__", _MAVEN_OPTS)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
        ] + [File(".", n, self._render(_STAGE_SH.replace("__APPLY__", a))) for n, a in _STAGES.items()]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            self._render(_PR_DOCKERFILE)
            .replace("__BASE_IMAGE__", self.dependency().image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


@Instance.register("OpenFeign", "feign")
@Instance.register("OpenFeign", _INTERVAL)
class FEIGN_3608_TO_3608(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Feign_3608_TO_3608ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_log(test_log)
