import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_JDK_IMAGE = "maven:3.8.8-eclipse-temurin-11"
_BASE_TAG = "base-4507_to_4417"

_NON_MODULE_DIRS = frozenset(
    {
        ".github",
        ".git",
        ".gitignore",
        ".gitattributes",
        ".mvn",
        "docs",
        "dist-material",
        "tools",
        "script",
        "docker",
        "helm",
        "assembly",
    }
)

_NO_TEST_MODULES = frozenset({"streampark-e2e", "streampark-shaded"})

_SKIP_FLAGS = (
    "-Dspotless.check.skip=true -Dspotless.skip=true "
    "-Dcheckstyle.skip=true -Dmaven.scalastyle.skip=true "
    "-Dscalastyle.skip=true -Drat.skip=true -Dlicense.skip=true "
    "-Denforcer.skip=true -Djacoco.skip=true -Dmaven.javadoc.skip=true "
    "-Dgpg.skip=true -Dnpm.skip=true -Dfrontend.skip=true"
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
"""

_EMIT_RESULTS_PY = r'''import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

repo = sys.argv[1]
rank = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
results = {}

patterns = [
    repo + "/**/target/surefire-reports/TEST-*.xml",
    repo + "/**/target/scalatest-reports/TEST-*.xml",
    repo + "/**/target/failsafe-reports/TEST-*.xml",
]

seen = set()
for pattern in patterns:
    for path in sorted(glob.glob(pattern, recursive=True)):
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            cls = (case.get("classname") or "").strip()
            raw = re.sub(r"\s+", " ", (case.get("name") or "")).strip()
            method = re.match(r"^([A-Za-z_$][\w$]*)\s*\(", raw)
            name = method.group(1) if method else raw
            if not cls or not name:
                continue
            status = "PASSED"
            for child in case:
                tag = child.tag.split("}")[-1].lower()
                if tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if tag == "skipped":
                    status = "SKIPPED"
                    break
            key = cls + "#" + name
            prev = results.get(key)
            if prev is None or rank[status] > rank[prev]:
                results[key] = status

for key in sorted(results):
    print("TESTCASE " + results[key] + " " + key)
sys.stderr.write("emit_results: %d test cases\n" % len(results))
'''

_PREPARE_SH = """#!/bin/bash
set -e

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e __BASE_SHA__^{commit} 2>/dev/null || git fetch --quiet --no-tags origin __BASE_SHA__
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__BASE_SHA__"

if [ -f .gitmodules ]; then
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true git submodule update --init --recursive || true
fi

test -x "$JAVA_HOME/bin/javac"

source /home/resolve_modules.sh

export MAVEN_OPTS="$MAVEN_OPTS -Dmaven.wagon.http.retryHandler.count=5 \
-Dmaven.wagon.http.retryHandler.requestSentEnabled=true \
-Dmaven.wagon.httpconnectionManager.ttlSeconds=30 \
-Dmaven.wagon.http.pool=false \
-Dmaven.wagon.rto=120000"

_warm() {
    for _attempt in 1 2 3 4; do
        _upd=""
        if [ "$_attempt" -gt 1 ]; then
            _upd="-U"
        fi
        if mvn -B -ntp $_upd dependency:go-offline -DskipTests __SKIP_FLAGS__ $PL_FLAG; then
            return 0
        fi
        echo "warm: pass $_attempt incomplete, backing off"
        sleep $((_attempt * 30))
    done
    return 1
}
_warm || true

mvn -B -ntp test -fn __SKIP_FLAGS__ $PL_FLAG || true
python3 /home/emit_results.py /home/__REPO__ >/dev/null || true
git submodule deinit -f --all 2>/dev/null || true
git reset --hard
git clean -fdx

test "$(git rev-parse HEAD)" = "__BASE_SHA__"
for _attempt in 1 2 3; do
    _upd=""
    if [ "$_attempt" -gt 1 ]; then
        _upd="-U"
    fi
    if mvn -B -ntp $_upd test-compile -DskipTests __SKIP_FLAGS__ $PL_FLAG; then
        break
    fi
    if [ "$_attempt" -eq 3 ]; then
        echo "gate: test-compile failed after $_attempt attempts" >&2
        exit 1
    fi
    echo "gate: attempt $_attempt failed, backing off"
    sleep $((_attempt * 45))
done
echo DEPS_OK
"""

_REACTOR_MODULES_PY = r'''import os
import xml.etree.ElementTree as ET

NS = "{http://maven.apache.org/POM/4.0.0}"

def child_modules(pom):
    try:
        root = ET.parse(pom).getroot()
    except Exception:
        return []
    names = []
    for tag in (NS + "modules", "modules"):
        for mods in root.findall(tag):
            for mod in list(mods):
                text = (mod.text or "").strip()
                if text:
                    names.append(text)
    return names

def main():
    seen = set()
    out = []
    stack = [""]
    while stack:
        rel = stack.pop()
        pom = os.path.join(rel, "pom.xml") if rel else "pom.xml"
        if pom in seen or not os.path.isfile(pom):
            continue
        seen.add(pom)
        if rel:
            out.append(rel.replace(os.sep, "/"))
        for name in child_modules(pom):
            stack.append(os.path.normpath(os.path.join(rel, name)))
    for path in out:
        print(path)

main()
'''

_RESOLVE_MODULES_SH = """export PL_FLAG=""
_MODULES="__MODULES__"
if [ -n "$_MODULES" ]; then
    _reactor=$(cd "/home/__REPO__" && python3 /home/reactor_modules.py 2>/dev/null)
    if [ -z "$_reactor" ]; then
        echo "module selector: FATAL could not read reactor" >&2
        exit 1
    fi

    _real=""
    for _m in $(echo "$_MODULES" | tr ',' ' '); do
        if [ ! -f "/home/__REPO__/$_m/pom.xml" ]; then
            continue
        fi
        if printf '%s\n' "$_reactor" | grep -qxF "$_m"; then
            _real="$_real $_m"
        else
            echo "module selector: dropping $_m (not in default reactor)"
        fi
    done

    _leaves=""
    for _m in $_real; do
        _is_parent=0
        for _n in $_real; do
            case "$_n" in
                "$_m") ;;
                "$_m"/*) _is_parent=1; break ;;
            esac
        done
        if [ "$_is_parent" -eq 0 ]; then
            if [ -z "$_leaves" ]; then
                _leaves="$_m"
            else
                _leaves="$_leaves,$_m"
            fi
        fi
    done

    if [ -n "$_leaves" ]; then
        PL_FLAG="-pl $_leaves -am"
    fi
fi
echo "module selector: $PL_FLAG"
"""

_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/__REPO__
find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true
find . -type d -name scalatest-reports -prune -exec rm -rf {} + 2>/dev/null || true

source /home/resolve_modules.sh

mvn -B -ntp test -fn __SKIP_FLAGS__ $PL_FLAG || true

python3 /home/emit_results.py /home/__REPO__
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

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

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

ENV MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC -Dfile.encoding=UTF-8" \\
    MAVEN_CONFIG=/root/.m2

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
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
    ca-certificates curl git python3 unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN update-ca-certificates && \\
    csplit -z -f /tmp/sysca- -b "%03d.pem" "${CA_CERT_PATH}" "/-----BEGIN CERTIFICATE-----/" "{*}" >/dev/null && \\
    for f in /tmp/sysca-*.pem; do \\
        keytool -importcert -noprompt -trustcacerts -cacerts -storepass changeit \\
            -alias "$(basename "$f" .pem)" -file "$f" >/dev/null 2>&1 || true; \\
    done && \\
    rm -f /tmp/sysca-*.pem && \\
    keytool -list -cacerts -storepass changeit | grep -c trustedCertEntry

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""

_PRUNE_BLOCK = r"""RUN set -eux; \
    git checkout --detach "__BASE_SHA__"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --quiet; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
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
            git gc --prune=now --quiet; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""

_TESTCASE_RE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S.*)$")

def _diff_paths(patch: str):
    for line in (patch or "").split("\n"):
        if not line.startswith("diff --git "):
            continue
        match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if not match:
            continue
        yield match.group(1)

def _extract_modules(patch: str) -> set[str]:
    modules: set[str] = set()
    for path in _diff_paths(patch):
        segments = path.split("/")
        if len(segments) < 2 or segments[0] in _NON_MODULE_DIRS:
            continue
        if "src" in segments:
            segments = segments[: segments.index("src")]
        else:
            segments = segments[:-1]
        for depth in range(1, len(segments) + 1):
            modules.add("/".join(segments[:depth]))
    return modules

def _build_modules(pr: PullRequest) -> str:
    modules = _extract_modules(pr.fix_patch) | _extract_modules(pr.test_patch)
    modules.discard("")
    kept = {m for m in modules if m.split("/")[0] not in _NO_TEST_MODULES}
    modules = kept or modules
    return ",".join(sorted(modules))

def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__SKIP_FLAGS__", _SKIP_FLAGS)
        .replace("__MODULES__", _build_modules(pr))
    )

class Streampark4507To4417ImageBase(Image):
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
        return _JDK_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        template = _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
        return (
            template.replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )

class Streampark4507To4417ImageDefault(Image):
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
        return Streampark4507To4417ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "emit_results.py", _EMIT_RESULTS_PY),
            File(".", "reactor_modules.py", _REACTOR_MODULES_PY),
            File(".", "resolve_modules.sh", _render(_RESOLVE_MODULES_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        prepare_copy = "".join(f"COPY {f.name} /home/\n" for f in self.prepare_files())
        graded_copy = "".join(f"COPY {f.name} /home/\n" for f in self.graded_files())

        sections = [f"FROM {image.image_name()}:{image.image_tag()}"]
        for part in (
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            prepare_copy,
            "RUN bash /home/prepare.sh",
            graded_copy,
            _render(_PRUNE_BLOCK, self.pr),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"

@Instance.register("apache", "streampark_4507_to_4417")
class STREAMPARK_4507_TO_4417(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Streampark4507To4417ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"PASSED": passed, "FAILED": failed, "SKIPPED": skipped}

        for line in test_log.replace("\r", "").split("\n"):
            match = _TESTCASE_RE.match(line.rstrip())
            if match:
                buckets[match.group(1)].add(match.group(2))

        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

Instance.register("apache", "streampark")(STREAMPARK_4507_TO_4417)
