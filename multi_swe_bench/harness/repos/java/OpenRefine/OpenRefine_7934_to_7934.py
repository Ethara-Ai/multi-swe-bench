import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "OpenRefine"
_REPO = "OpenRefine"
_INTERVAL = "OpenRefine_7934_to_7934"

# The only credited test is a Cypress e2e spec
# (main/tests/cypress/cypress/e2e/**/*.cy.js). It needs a running OpenRefine
# server (built with Maven) plus Cypress' bundled Electron browser, exactly as
# .github/workflows/pull_request_e2e.yml + `./refine e2e_tests` do.
#
# JDK: pull_request_e2e.yml sets up Temurin 21 and pom.xml declares
# java.minversion=21. The Docker official image is referenced through its ECR
# Public mirror (same multi-arch manifest as docker.io/library/eclipse-temurin)
# to avoid Docker Hub's anonymous pull rate limit.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:21.0.10_7-jdk-noble"

# Maven: the repo has no wrapper; `./refine` uses the mvn on PATH.
_MAVEN_VERSION = "3.9.9"
_MAVEN_SHA512 = (
    "a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556"
    "bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b"
)

# Node: main/webapp/package.json and main/tests/cypress/package.json declare
# engines node>=24 / npm>=11.16 and CI uses node-version 'lts/*'. v24.20.0
# (npm 11.19.0) is the Node 24 LTS release current at the base commit
# (2026-08-28). Checksums are from nodejs.org's SHASUMS256.txt.
_NODE_VERSION = "24.20.0"
_NODE_SHA256_AMD64 = "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2"
_NODE_SHA256_ARM64 = "5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7"

# Cypress (Electron) runtime libraries + Xvfb, per Cypress' Linux prerequisites
# for Ubuntu 24.04 (t64 package names). xz-utils unpacks the Node tarball.
_BASE_APT = (
    "git ca-certificates p11-kit curl xz-utils procps "
    "xvfb xauth libgtk-3-0t64 libgtk2.0-0t64 libgbm1 libnotify4 libnss3 "
    "libxss1 libasound2t64 libxtst6 fonts-liberation"
)

_MVN_FLAGS = "-B -ntp -DskipTests -Dmaven.javadoc.skip=true"

_TIMEOUT = "timeout -k 60 2400"
_TIMEOUT_PREPARE = "timeout -k 60 3600"

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
}
"""

# Test selection: only the Cypress specs test.patch touches that exist at this
# stage (paths relative to main/tests/cypress, as `./refine e2e_tests` passes
# them to --spec).
_DERIVE_TARGETS = r"""SPECS=""
for _f in $(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/test.patch | sort -u); do
    [ -f "$_f" ] || continue
    case "$_f" in
        main/tests/cypress/cypress/e2e/*.cy.js|main/tests/cypress/cypress/e2e/*.cy.ts)
            SPECS="$SPECS,${_f#main/tests/cypress/}" ;;
    esac
done
SPECS="${SPECS#,}"
echo "selected cypress specs: [$SPECS]"
"""

# Mirrors `./refine e2e_tests`: headless server on 127.0.0.1:3333 with a
# throw-away data dir, then `cypress run --browser electron --headless` with the
# same --env. The JUnit reporter replaces `--reporter list` so every test case
# is reported by name; each spec's XML carries its spec path (file="...").
# The webapp (html/js/css) is served from main/webapp, so fix.patch changes are
# live without a rebuild; Java sources are recompiled only if a patch touches
# them.
_RUN_TARGETS = r"""if [ -z "$SPECS" ]; then
    echo "NO_TESTS_AT_THIS_STAGE"
    exit 0
fi

if git status --porcelain --untracked-files=all | awk '{print $NF}' | grep -E '\.java$' > /dev/null; then
    @@TIMEOUT@@ mvn @@MVN_FLAGS@@ -o compile
fi

rm -rf /tmp/openrefine-tests /tmp/cypress-results
mkdir -p /tmp/openrefine-tests /tmp/cypress-results
./refine -i 127.0.0.1 -p 3333 -d /tmp/openrefine-tests -m 2048M -x refine.headless=true run > /tmp/openrefine-server.log 2>&1 &
REFINE_PID=$!
_ready=0
for _i in $(seq 1 180); do
    if curl -fs --noproxy '*' http://127.0.0.1:3333/ | grep "<title>OpenRefine</title>" > /dev/null; then
        _ready=1
        break
    fi
    sleep 1
done
if [ "$_ready" != 1 ]; then
    echo "OPENREFINE_SERVER_DID_NOT_START"
    tail -n 50 /tmp/openrefine-server.log
    kill "$REFINE_PID" 2>/dev/null || true
    exit 1
fi
echo "OpenRefine server is up"

RC=0
cd @@REPO_DIR@@/main/tests/cypress
@@TIMEOUT@@ yarn run cypress run --spec "$SPECS" --browser electron --headless \
    --reporter junit \
    --reporter-options "mochaFile=/tmp/cypress-results/result-[hash].xml,toConsole=false" \
    --env DISABLE_PROJECT_CLEANUP=1,OPENREFINE_URL=http://127.0.0.1:3333 || RC=$?
cd @@REPO_DIR@@

kill "$REFINE_PID" 2>/dev/null || true
wait "$REFINE_PID" 2>/dev/null || true

for _x in /tmp/cypress-results/*.xml; do
    [ -f "$_x" ] || continue
    echo "CYPRESS_JUNIT_BEGIN"
    cat "$_x"
    echo
    echo "CYPRESS_JUNIT_END"
done
echo "TEST_RUNNER_EXIT_CODE=$RC"
# Propagate cypress' status after the JUnit XML has been printed, so a runner
# that crashed or could not start fails the stage instead of exiting 0.
exit $RC
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail
export CI=true

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd @@REPO_DIR@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "@@SHA@@"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "@@SHA@@"

# ---------- Section 2: PROVISION (build time, pinned by this commit) ----------
# `./refine build` = (cd main/webapp && npm install) + mvn process-resources +
# mvn compile test-compile. `npm ci` is used instead of `npm install` so the
# committed package-lock.json is honoured and left untouched; its postinstall
# (copy-dependencies.js) fills the gitignored modules/core/3rdparty.
cd @@REPO_DIR@@/main/webapp
npm ci --no-audit --no-fund
cd @@REPO_DIR@@
@@TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ process-resources
@@TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ compile test-compile
# Run the offline-compile path used by the run scripts once, so every plugin it
# needs is cached.
@@TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ compile

# Cypress suite: yarn 4 (packageManager yarn@4.18.0, via corepack) with the
# committed yarn.lock. Yarn 4.18 does not run dependency build scripts
# ("cypress lists build scripts, but all build scripts have been disabled"), so
# the binary that cypress' postinstall would fetch is installed explicitly:
# `cypress install` downloads the 15.21.1 binary (bundled Electron) matching
# the locked npm package for this architecture into ~/.cache/Cypress.
# The base ENV sets YARN_ENABLE_IMMUTABLE_INSTALLS=false (QC 2B); the explicit
# `--immutable` flag below is deliberate and wins over it: it is what the repo's
# CI runs for this suite, and it makes the build fail loudly if the committed
# yarn.lock does not resolve unchanged, rather than silently re-resolving to
# versions newer than this commit (era drift).
cd @@REPO_DIR@@/main/tests/cypress
yarn install --immutable
yarn run cypress install
cd @@REPO_DIR@@

# ---------- Section 3: HARD GATE (non-tolerant) ----------
test -d @@REPO_DIR@@/server/classes
test -d @@REPO_DIR@@/main/webapp/modules/core/3rdparty
@@TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ -o -q compile
cd @@REPO_DIR@@/main/tests/cypress
test "$(yarn --version)" = "4.18.0"
yarn run cypress verify
cd @@REPO_DIR@@
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
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \
    npm_config_cafile=${CA_CERT_PATH} \
    npm_config_update_notifier=false \
    YARN_HTTPS_CA_FILE_PATH=${CA_CERT_PATH} \
    YARN_ENABLE_TELEMETRY=0 \
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CYPRESS_CRASH_REPORTS=0 \
    CYPRESS_COMMERCIAL_RECOMMENDATIONS=0 \
    NO_COLOR=1 \
    FORCE_COLOR=0
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    @@APT@@ \
    && rm -rf /var/lib/apt/lists/*

# Trust the system CA bundle (including any MITM proxy CA it carries) in the
# JDK's own truststore, which Maven uses instead of /etc/ssl/certs.
# p11-kit's trust module reads /etc/ssl/certs/ca-certificates.crt (the
# CA_CERT_PATH every symlink above points at) and writes the whole set as a
# JKS in one pass: fast and arch-independent, no per-cert keytool loop and
# no tolerated failures. The count check is the assertion.
RUN set -eux; \
    p11-kit extract --format=java-cacerts --purpose=server-auth --overwrite \
        "${JAVA_HOME}/lib/security/cacerts"; \
    chmod 644 "${JAVA_HOME}/lib/security/cacerts"; \
    test "$(keytool -list -cacerts -storepass changeit | grep -c 'trustedCertEntry')" -gt 100

ENV MAVEN_HOME=/opt/apache-maven-@@MAVEN_VERSION@@ \
    MAVEN_OPTS="-Xmx3g"

RUN set -eux; \
    curl -fsSL -o /tmp/maven.tar.gz \
        "https://archive.apache.org/dist/maven/maven-3/@@MAVEN_VERSION@@/binaries/apache-maven-@@MAVEN_VERSION@@-bin.tar.gz"; \
    echo "@@MAVEN_SHA512@@  /tmp/maven.tar.gz" | sha512sum -c -; \
    tar -xzf /tmp/maven.tar.gz -C /opt; \
    ln -s /opt/apache-maven-@@MAVEN_VERSION@@/bin/mvn /usr/local/bin/mvn; \
    rm -f /tmp/maven.tar.gz; \
    java -version; \
    mvn -v

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) node_arch=x64; node_sha=@@NODE_SHA256_AMD64@@ ;; \
        arm64) node_arch=arm64; node_sha=@@NODE_SHA256_ARM64@@ ;; \
        *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.xz \
        "https://nodejs.org/dist/v@@NODE_VERSION@@/node-v@@NODE_VERSION@@-linux-${node_arch}.tar.xz"; \
    echo "${node_sha}  /tmp/node.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f /tmp/node.tar.xz; \
    test "$(node --version)" = "v@@NODE_VERSION@@"; \
    npm --version; \
    corepack enable; \
    git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@
@@CLEAR_ENV@@
CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM @@BASE_IMAGE@@
@@GLOBAL_ENV@@
@@COPY@@

RUN bash /home/prepare.sh

RUN set -eux; \
    cd @@REPO_DIR@@; \
    git checkout --detach "@@SHA@@"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "@@SHA@@"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f @@REPO_DIR@@/.gitmodules ]; then \
        cd @@REPO_DIR@@ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
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


class OpenRefine7934To7934ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                'RUN git clone "${REPO_URL}" /home/%s && \\\n'
                "    cd /home/%s && git rev-parse HEAD >/dev/null"
            ) % (repo, repo)
        else:
            code = "COPY %s /home/%s" % (repo, repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": self.dependency(),
                "ORG": self.pr.org,
                "REPO": repo,
                "APT": _BASE_APT,
                "MAVEN_VERSION": _MAVEN_VERSION,
                "MAVEN_SHA512": _MAVEN_SHA512,
                "NODE_VERSION": _NODE_VERSION,
                "NODE_SHA256_AMD64": _NODE_SHA256_AMD64,
                "NODE_SHA256_ARM64": _NODE_SHA256_ARM64,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class OpenRefine7934To7934ImageDefault(Image):
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
        return OpenRefine7934To7934ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "MVN_FLAGS": _MVN_FLAGS,
            "TIMEOUT_PREPARE": _TIMEOUT_PREPARE,
            "TIMEOUT": _TIMEOUT,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "APPLY_FN": _APPLY_FN,
                "DERIVE_TARGETS": _DERIVE_TARGETS,
                "RUN_TARGETS": _RUN_TARGETS,
            },
        )
        return _render(body, self._values())

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._script(_PREPARE)),
            File(".", "run.sh", self._script(_RUN)),
            File(".", "test-run.sh", self._script(_TEST_RUN)),
            File(".", "fix-run.sh", self._script(_FIX_RUN)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        values = self._values()
        values.update(
            {
                "BASE_IMAGE": "%s:%s" % (base.image_name(), base.image_tag()),
                "COPY": copy_commands,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            }
        )
        return _render(_PR_DOCKERFILE, values)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_DOC = re.compile(r"CYPRESS_JUNIT_BEGIN\n(.*?)\nCYPRESS_JUNIT_END", re.DOTALL)
_SPEC_FILE = re.compile(r'<testsuite\b[^>]*\bfile="([^"]+)"')
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_NAME = re.compile(r'\bname="([^"]*)"')
_XML_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&"))


def _unescape(s: str) -> str:
    for k, v in _XML_ENTITIES:
        s = s.replace(k, v)
    return s


def _parse(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log).replace("\r\n", "\n")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for doc in _DOC.findall(clean):
        fm = _SPEC_FILE.search(doc)
        spec = _unescape(fm.group(1)).strip() if fm else "unknown-spec"
        for m in _TESTCASE.finditer(doc):
            nm = _NAME.search(m.group(1))
            if not nm:
                continue
            title = " ".join(_unescape(nm.group(1)).split())
            test_id = "%s > %s" % (spec, title)
            inner = m.group(3) or ""
            if m.group(2) == "/>":
                passed.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed.add(test_id)
            elif "<skipped" in inner:
                skipped.add(test_id)
            else:
                passed.add(test_id)

    passed -= failed
    skipped -= failed
    passed -= skipped

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register(_ORG, _INTERVAL)
class OPENREFINE_7934_TO_7934(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OpenRefine7934To7934ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse(test_log)
