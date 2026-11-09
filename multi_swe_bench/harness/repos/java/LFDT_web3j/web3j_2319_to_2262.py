from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "eclipse-temurin:21-jdk"
_BASE_TAG = "base-2319_to_2262"
_REPO_DIR = "/home/web3j"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MODULE_RE = re.compile(r"^=== MSB_MODULE: (\S+) (\S+) ===$", re.M)
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="

_TEST_BODY_SH = """find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

rc=0
( cd "$GRADLE_DIR" && timeout 5400 bash ./gradlew test --no-daemon --console=plain --continue ) \
    > /tmp/msb-gradle.log 2>&1 || rc=$?
cat /tmp/msb-gradle.log

echo "===== TEST REPORTS BEGIN ====="
find . -path '*/build/test-results/*' -name 'TEST-*.xml' \
    | sort \
    | while read -r report; do
        module="${report#./}"
        module="${module%%/build/*}"
        printf '\\n=== MSB_MODULE: %s src/test/java ===\\n' "$module"
        cat "$report"
    done
echo "===== TEST REPORTS END ====="

exit "$rc"
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -e

REPO_ROOT=/home/web3j
GRADLE_DIR=$REPO_ROOT

cd "$REPO_ROOT"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

export CI=true
export GRADLE_USER_HOME=/root/.gradle
mkdir -p "$GRADLE_USER_HOME"
cat > "$GRADLE_USER_HOME/gradle.properties" <<'PROPEOF'
org.gradle.daemon=false
org.gradle.console=plain
org.gradle.jvmargs=-Xmx3g -XX:MaxMetaspaceSize=768m -Dfile.encoding=UTF-8
org.gradle.java.installations.auto-download=false
PROPEOF

mkdir -p "$GRADLE_USER_HOME/init.d"
cat > "$GRADLE_USER_HOME/init.d/msb-names.gradle" <<'INITEOF'
allprojects {
    tasks.withType(Test).configureEach {
        systemProperty 'junit.jupiter.params.displayname.default', '{displayName} [{index}] {argumentsWithNames}'
    }
}
INITEOF

if [ -n "$https_proxy$http_proxy" ]; then
    python3 - "$GRADLE_USER_HOME/gradle.properties" <<'PROXYEOF'
import os
import sys
from urllib.parse import urlparse

lines = []
for scheme, env in (("http", "http_proxy"), ("https", "https_proxy")):
    raw = os.environ.get(env) or os.environ.get(env.upper()) or ""
    if not raw:
        continue
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        continue
    lines.append("systemProp." + scheme + ".proxyHost=" + parsed.hostname)
    lines.append("systemProp." + scheme + ".proxyPort=" + str(parsed.port or 8080))
    skip = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    if skip:
        joined = "|".join(part.strip() for part in skip.split(",") if part.strip())
        lines.append("systemProp." + scheme + ".nonProxyHosts=" + joined)
if lines:
    with open(sys.argv[1], "a", encoding="utf-8") as handle:
        handle.write(chr(10).join(lines) + chr(10))
PROXYEOF
fi

attempt=1
while [ "$attempt" -le 3 ]; do
    if ( cd "$GRADLE_DIR" && timeout 7200 bash ./gradlew test --no-daemon --console=plain --continue ) \
        > /tmp/msb-warm.log 2>&1; then
        break
    fi
    tail -40 /tmp/msb-warm.log
    if grep -qE 'Could not resolve|Could not download|Could not GET|Connection reset|Read timed out|returned status code 5|Premature end of Content-Length' /tmp/msb-warm.log; then
        attempt=$((attempt + 1))
        sleep 15
        continue
    fi
    break
done

find . -path '*/build/test-results/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

gate=0
( cd "$GRADLE_DIR" && timeout 7200 bash ./gradlew test --offline --no-daemon --console=plain --continue ) \
    > /tmp/msb-gate.log 2>&1 || gate=$?
reports=$(find . -path '*/build/test-results/*' -name 'TEST-*.xml' | wc -l)
tail -40 /tmp/msb-gate.log
echo "gate exit: $gate, gate reports: $reports"

if grep -qE 'No cached version|Could not resolve|Could not download|No locked version|Could not find' /tmp/msb-gate.log; then
    echo "prepare.sh: offline dependency resolution failed"
    exit 1
fi
if grep -qE 'Compilation failed|No matching toolchains found|Could not create task|Script compilation error' /tmp/msb-gate.log; then
    echo "prepare.sh: offline gate build failure"
    exit 1
fi
if [ "$reports" -eq 0 ]; then
    test "$gate" -eq 0
fi

echo "DEPS_OK"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export GRADLE_USER_HOME=/root/.gradle

REPO_ROOT=/home/web3j
GRADLE_DIR=$REPO_ROOT

cd "$REPO_ROOT"
"""

_RUN_SH = f"""{_HEADER_SH}
{_TEST_BODY_SH}
"""

_TEST_RUN_SH = f"""{_HEADER_SH}
git apply --whitespace=nowarn /home/test.patch

{_TEST_BODY_SH}
"""

_FIX_RUN_SH = f"""{_HEADER_SH}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{_TEST_BODY_SH}
"""


def _report_section(clean_log: str) -> str:
    start = clean_log.find(_REPORTS_BEGIN)
    if start < 0:
        return ""
    stop = clean_log.find(_REPORTS_END, start)
    if stop < 0:
        return clean_log[start:]
    return clean_log[start:stop]


def _case_id(module: str, root: str, classname: str, name: str) -> str:
    parts = classname.split("$")
    path = parts[0].replace(".", "/") + ".java"
    label = ".".join(parts[1:] + [name])
    prefix = "" if module in ("", ".") else module + "/"
    return prefix + root + "/" + path + "::" + label


class Web3j2319To2262ImageBase(Image):
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
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    CI=true \\
    JAVA_HOME=/opt/java/openjdk \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} web3j_2319_to_2262 instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /usr/local/share/ca-certificates && \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cert.pem && \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem && \\
    ln -sf ${{CA_CERT_PATH}} /usr/local/share/ca-certificates/ca-certificates.crt && \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN --mount=type=secret,id=mitm_ca,required=0 \\
    if [ -s /run/secrets/mitm_ca ]; then \\
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm_ca.crt && \\
        update-ca-certificates && \\
        keytool -importcert -noprompt -storepass changeit \\
            -alias msb-mitm -file /run/secrets/mitm_ca \\
            -keystore "$(ls -1 $JAVA_HOME/jre/lib/security/cacerts $JAVA_HOME/lib/security/cacerts 2>/dev/null | head -1)"; \\
    fi

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    python3 \\
    unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Web3j2319To2262ImageDefault(Image):
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
        return Web3j2319To2262ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                _CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.replace("__BASE_SHA__", self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH,
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH,
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd {_REPO_DIR}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    rm -f .git/ORIG_HEAD; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {_REPO_DIR}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("LFDT-web3j", "web3j_2319_to_2262")
class WEB3J_2319_TO_2262(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Web3j2319To2262ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log)
        section = _report_section(clean_log)
        markers = [
            (match.start(), match.group(1), match.group(2))
            for match in _MODULE_RE.finditer(section)
        ]

        for case in _CASE_RE.finditer(section):
            attrs = dict(_ATTR_RE.findall(case.group(1)))
            classname = attrs.get("classname", "")
            name = attrs.get("name", "")
            if not classname or not name:
                continue

            module = ""
            root = "src/test/java"
            for position, candidate, candidate_root in markers:
                if position < case.start():
                    module = candidate
                    root = candidate_root
                else:
                    break

            test_id = _case_id(module, root, classname, name)
            body = case.group(3) or ""
            if "<failure" in body or "<error" in body:
                failed_tests.add(test_id)
            elif "<skipped" in body:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

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
