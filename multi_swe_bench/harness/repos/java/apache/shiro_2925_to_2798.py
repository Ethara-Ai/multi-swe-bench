from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "maven:3.9-eclipse-temurin-25"
_BASE_TAG = "base-2925_to_2798"
_REPO_DIR = "/home/shiro"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_MODULE_RE = re.compile(r"^=== MSB_MODULE: (\S+) (\S+) ===$", re.M)
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="

_MVN_FLAGS = (
    "-B -ntp -fae"
    " -Dmaven.test.failure.ignore=true"
    " -DfailIfNoTests=false"
    " -Dsurefire.failIfNoSpecifiedTests=false"
    " -Dcheckstyle.skip=true"
    " -Drat.skip=true"
    " -Dspotbugs.skip=true"
    " -Dlicense.skip=true"
    " -Dspotless.check.skip=true"
    " -Denforcer.skip=true"
    " -Ddependency-check.skip=true"
    " -Djacoco.skip=true"
    " -Dmaven.javadoc.skip=true"
    " -Danimal.sniffer.skip=true"
    " -Dmaven.wagon.http.retryHandler.count=3"
    " -Dmaven.wagon.httpconnectionManager.ttlSeconds=120"
    " -Djunit5.parallel.tests.enabled=false"
    " -DskipITs=true"
    " -Djapicmp.skip=true"
)

_DERIVE_SH = """python3 - "$REPO_ROOT" /home/test.patch /home/msb-sel.txt /home/msb-roots.txt /home/msb-tests.txt <<'PYEOF'
import os
import re
import sys

root = os.path.realpath(sys.argv[1])
OPEN = chr(123)
CLOSE = chr(125)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


reactor = []
seen = []
queue = [root]
while queue:
    current = queue.pop(0)
    pom = os.path.join(current, "pom.xml")
    if current in seen or not os.path.isfile(pom):
        continue
    seen.append(current)
    rel = os.path.relpath(current, root)
    if rel != ".":
        reactor.append(rel)
    for name in re.findall(r"<module>\\s*([^<]+?)\\s*</module>", strip_comments(read(pom))):
        queue.append(os.path.realpath(os.path.join(current, name)))

paths = sorted(set(re.findall(r"^\\+\\+\\+ b/(.+)$", read(sys.argv[2]), re.M)))
modules = []
for path in paths:
    current = os.path.dirname(os.path.realpath(os.path.join(root, path)))
    while current.startswith(root):
        if os.path.isfile(os.path.join(current, "pom.xml")):
            rel = os.path.relpath(current, root)
            if rel in reactor and rel not in modules:
                modules.append(rel)
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

roots = []
for rel in modules:
    text = strip_comments(read(os.path.join(root, rel, "pom.xml")))
    found = re.findall(r"<testSourceDirectory>\\s*([^<]+?)\\s*</testSourceDirectory>", text)
    value = found[0] if found else "src/test/java"
    value = value.replace("$" + OPEN + "project.basedir" + CLOSE + "/", "")
    roots.append(rel + "\\t" + value.strip("/"))

classes = []
for path in paths:
    name = os.path.basename(path)
    if not name.endswith(".java"):
        continue
    marked = "/" + path
    if "/src/test/" not in marked and "/test/jvm/" not in marked:
        continue
    stem = name[:-5]
    if stem not in classes:
        classes.append(stem)

with open(sys.argv[3], "w", encoding="utf-8") as handle:
    handle.write(",".join(modules) + "\\n")
with open(sys.argv[4], "w", encoding="utf-8") as handle:
    handle.write("\\n".join(roots) + "\\n")
with open(sys.argv[5], "w", encoding="utf-8") as handle:
    handle.write(",".join(classes) + "\\n")
PYEOF
SEL=$(cat /home/msb-sel.txt)
TESTARG=$(cat /home/msb-tests.txt)
echo "selected modules: $SEL"
echo "selected classes: $TESTARG"
"""

_LOAD_SH = """SEL=$(cat /home/msb-sel.txt)
TESTARG=$(cat /home/msb-tests.txt)
echo "selected modules: $SEL"
echo "selected classes: $TESTARG"
"""

_TEST_BODY_SH = f"""{_LOAD_SH}
if [ -z "$SEL" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    echo "===== TEST REPORTS BEGIN ====="
    echo "===== TEST REPORTS END ====="
    exit 0
fi

find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

build=0
timeout 3600 mvn {_MVN_FLAGS} -DskipTests install -pl "$SEL" -am > /tmp/msb-build.log 2>&1 || build=$?
cat /tmp/msb-build.log
echo "upstream build exit: $build"

rc=0
timeout 3600 mvn {_MVN_FLAGS} -pl "$SEL" test > /tmp/msb-mvn.log 2>&1 || rc=$?
cat /tmp/msb-mvn.log

echo "===== TEST REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' ! -name 'TEST-TestSuite.xml' \
    | sort \
    | while read -r report; do
        module="${{report#./}}"
        module="${{module%%/target/*}}"
        troot=$(awk -F'\\t' -v m="$module" '$1 == m {{ print $2 }}' /home/msb-roots.txt | head -1)
        [ -n "$troot" ] || troot=src/test/java
        printf '\\n=== MSB_MODULE: %s %s ===\\n' "$module" "$troot"
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

_PREPARE_SH = f"""#!/bin/bash
set -e

REPO_ROOT=/home/shiro

cd "$REPO_ROOT"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"

if [ -n "$https_proxy$http_proxy" ]; then
    mkdir -p /root/.m2
    python3 - /root/.m2/settings.xml <<'PROXYEOF'
import os
import sys
from urllib.parse import urlparse

entries = []
for scheme, env in (("http", "http_proxy"), ("https", "https_proxy")):
    raw = os.environ.get(env) or os.environ.get(env.upper()) or ""
    if not raw:
        continue
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        continue
    skip = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    joined = "|".join(part.strip() for part in skip.split(",") if part.strip())
    entries.append(
        "    <proxy><id>msb-" + scheme + "</id><active>true</active>"
        "<protocol>" + scheme + "</protocol>"
        "<host>" + parsed.hostname + "</host>"
        "<port>" + str(parsed.port or 8080) + "</port>"
        "<nonProxyHosts>" + joined + "</nonProxyHosts></proxy>"
    )
if entries:
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        handle.write("<settings><proxies>" + chr(10))
        handle.write(chr(10).join(entries) + chr(10))
        handle.write("</proxies></settings>" + chr(10))
PROXYEOF
fi
{_DERIVE_SH}
test -n "$SEL"

attempt=1
while [ "$attempt" -le 3 ]; do
    if timeout 7200 mvn {_MVN_FLAGS} -DskipTests install > /tmp/msb-install.log 2>&1; then
        break
    fi
    tail -40 /tmp/msb-install.log
    if grep -qE 'Could not resolve dependencies|Could not transfer artifact|Connection reset|Connection timed out|Read timed out|Premature end of Content-Length' /tmp/msb-install.log; then
        attempt=$((attempt + 1))
        sleep 15
        continue
    fi
    break
done

timeout 7200 mvn {_MVN_FLAGS} -DskipTests install -pl "$SEL" -am > /tmp/msb-install-sel.log 2>&1 || true
tail -20 /tmp/msb-install-sel.log

attempt=1
while [ "$attempt" -le 3 ]; do
    if timeout 5400 mvn {_MVN_FLAGS} -pl "$SEL" test > /tmp/msb-warm.log 2>&1; then
        break
    fi
    tail -40 /tmp/msb-warm.log
    if grep -qE 'Could not resolve dependencies|Could not transfer artifact|Connection reset|Connection timed out|Read timed out|Premature end of Content-Length' /tmp/msb-warm.log; then
        attempt=$((attempt + 1))
        sleep 15
        continue
    fi
    break
done

find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' -delete 2>/dev/null || true

build=0
timeout 5400 mvn -o {_MVN_FLAGS} -DskipTests install -pl "$SEL" -am > /tmp/msb-gate.log 2>&1 || build=$?
gate=0
timeout 5400 mvn -o {_MVN_FLAGS} -pl "$SEL" test >> /tmp/msb-gate.log 2>&1 || gate=$?
reports=$(find . -path '*/target/surefire-reports/*' -name 'TEST-*.xml' | wc -l)
tail -40 /tmp/msb-gate.log
echo "gate build exit: $build, gate exit: $gate, gate reports: $reports"

if grep -qE 'Could not resolve dependencies|Could not transfer artifact|Cannot access [^ ]+ in offline mode|could not be resolved' /tmp/msb-gate.log; then
    echo "prepare.sh: offline dependency resolution failed"
    exit 1
fi
test "$build" -eq 0
test "$gate" -eq 0

echo "DEPS_OK"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export MAVEN_OPTS="-Xmx4g -XX:MaxMetaspaceSize=512m"

REPO_ROOT=/home/shiro

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


class Shiro2925To2798ImageBase(Image):
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
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} shiro_2925_to_2798 instances"
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
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Shiro2925To2798ImageDefault(Image):
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
        return Shiro2925To2798ImageBase(self.pr, self._config)

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


@Instance.register("apache", "shiro_2925_to_2798")
class SHIRO_2925_TO_2798(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Shiro2925To2798ImageDefault(self.pr, self._config)

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
