import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "unitycatalog"
_REPO = "unitycatalog"
_INTERVAL = "unitycatalog_2013_to_2013"

# JDK: build.sbt compiles the server with `--release 17` and every CI workflow
# (.github/workflows/ucai-core-tests.yml, unit-tests.yml) sets up Temurin 17.
# Python: ucai-core-tests.yml uses python-version '3.10' -> Ubuntu jammy's
# system python3 is 3.10. The Docker official image is referenced through its
# ECR Public mirror (same multi-arch manifest as
# docker.io/library/eclipse-temurin) to avoid Docker Hub's anonymous pull rate
# limit.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:17.0.20_8-jdk-jammy"
_PYTHON_VERSION = "3.10"

# uv: .github/scripts/install-uv.sh pins uv 0.10.12 (static musl build,
# SHA256-verified). The script hardcodes x86_64; both arch checksums are the
# official release `.sha256` assets.
_UV_VERSION = "0.10.12"
_UV_SHA256_AMD64 = "adccf40b5d1939a5e0093081ec2307ea24235adf7c2d96b122c561fa37711c46"
_UV_SHA256_ARM64 = "55bd1c1c10ec8b95a8c184f5e18b566703c6ab105f0fc118aaa4d748aabf28e4"

# ucai-core-tests.yml / ai/dev/install_client.sh / ai/core/pyproject.toml
# ([tool.uv] exclude-newer) all pin the resolver horizon to this instant, so
# unpinned (non-lockfile) resolution stays at the commit's era.
_UV_EXCLUDE_NEWER = "2026-03-10T00:00:00Z"

# sbt: project/build.properties pins sbt.version=1.9.9; the repo's own
# build/sbt launcher script downloads exactly that launcher.
_SBT_FLAGS = "-J-Xmx3G -J-XX:+UseG1GC"

_BASE_APT = "git ca-certificates p11-kit curl python3 python3-dev build-essential"

_TIMEOUT = "timeout -k 60 2700"
_TIMEOUT_PREPARE = "timeout -k 60 5400"

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

# Test selection: only the test files test.patch touches that exist at this
# stage.
#   - ai/core/tests/**/test_*.py -> pytest, run from ai/core (its
#     pyproject sets pythonpath=["src"], so the tree's sources are tested).
#   - server/src/test/java/**/*.java -> `sbt server/testOnly`. An abstract
#     Base* test (the build's Tests.Filter excludes io.unitycatalog.server.base)
#     is replaced by the concrete server test classes that extend it.
_DERIVE_TARGETS = r"""PY_TESTS=""
JAVA_CLASSES=""
_java_cls() {
    printf '%s' "$1" | sed -E 's#^server/src/test/java/##; s#\.java$##; s#/#.#g'
}
for _f in $(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/test.patch | sort -u); do
    [ -f "$_f" ] || continue
    case "$_f" in
        ai/core/tests/*test_*.py) PY_TESTS="$PY_TESTS ${_f#ai/core/}" ;;
        server/src/test/java/*.java)
            _name="$(basename "$_f" .java)"
            if grep -qE "abstract[[:space:]]+class[[:space:]]+$_name\b" "$_f"; then
                for _sub in $(grep -rlE "extends[[:space:]]+$_name\b" server/src/test/java | sort -u); do
                    grep -qE "abstract[[:space:]]+class" "$_sub" && continue
                    JAVA_CLASSES="$JAVA_CLASSES $(_java_cls "$_sub")"
                done
            else
                JAVA_CLASSES="$JAVA_CLASSES $(_java_cls "$_f")"
            fi
            ;;
    esac
done
JAVA_CLASSES="$(printf '%s\n' $JAVA_CLASSES | sort -u | paste -sd' ' -)"
echo "selected: pytest=[${PY_TESTS# }] sbt server/testOnly=[${JAVA_CLASSES}]"
"""

# A non-zero runner exit only means "some test failed"; results are reported
# per test case from the JUnit XML both runners write, so the stage carries on
# to the second runner, then propagates the collected status (`exit $RC`) so
# a runner that crashed or could not start fails the stage.
_RUN_TARGETS = r"""if [ -z "$PY_TESTS" ] && [ -z "$JAVA_CLASSES" ]; then
    echo "NO_TESTS_AT_THIS_STAGE"
    exit 0
fi

RC=0
if [ -n "$PY_TESTS" ]; then
    rm -f /tmp/pytest-junit.xml
    cd @@REPO_DIR@@/ai/core
    @@TIMEOUT@@ python3 -m pytest -p no:cacheprovider -o junit_family=xunit1 \
        --junitxml=/tmp/pytest-junit.xml $PY_TESTS || RC=$?
    cd @@REPO_DIR@@
    if [ -f /tmp/pytest-junit.xml ]; then cat /tmp/pytest-junit.xml; echo; fi
fi

if [ -n "$JAVA_CLASSES" ]; then
    rm -rf @@REPO_DIR@@/server/target/test-reports
    @@TIMEOUT@@ build/sbt @@SBT_FLAGS@@ "server/testOnly $JAVA_CLASSES" || RC=$?
    if [ -d @@REPO_DIR@@/server/target/test-reports ]; then
        find @@REPO_DIR@@/server/target/test-reports -name 'TEST-*.xml' -exec cat {} +
    fi
fi

echo "TEST_RUNNER_EXIT_CODE=$RC"
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
# Python deps exactly as ucai-core-tests.yml: hash-locked dev requirements,
# then the repo-generated unitycatalog-client and unitycatalog-ai core
# (ai/dev/install_client.sh). sbt resolves + compiles the server test classpath
# and generates the Python client in the same JVM session.
uv pip install --system --require-hashes -r ai/core/requirements-dev.lock
@@TIMEOUT_PREPARE@@ build/sbt @@SBT_FLAGS@@ pythonClient/generate server/Test/compile
uv pip install --system "./clients/python/target/.[dev]"
uv pip install --system "./ai/core/.[dev]"

# ---------- Section 3: HARD GATE (offline resolution, non-tolerant) ----------
# The test classpath must resolve and compile from the local caches alone.
COURSIER_MODE=offline @@TIMEOUT_PREPARE@@ build/sbt @@SBT_FLAGS@@ server/Test/compile
cd @@REPO_DIR@@/ai/core
python3 -c "import unitycatalog.client, unitycatalog.ai.core.utils.callable_utils, pydantic, pytest"
python3 -m pytest -p no:cacheprovider --collect-only -q tests/core/test_callable_utils.py > /dev/null
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
    UV_NATIVE_TLS=1 \
    UV_EXCLUDE_NEWER=@@UV_EXCLUDE_NEWER@@ \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
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
# JDK's own truststore, which sbt/coursier use instead of /etc/ssl/certs.
# p11-kit's trust module reads /etc/ssl/certs/ca-certificates.crt (the
# CA_CERT_PATH every symlink above points at) and writes the whole set as a
# JKS in one pass: fast and arch-independent, no per-cert keytool loop and
# no tolerated failures. The count check is the assertion.
RUN set -eux; \
    p11-kit extract --format=java-cacerts --purpose=server-auth --overwrite \
        "${JAVA_HOME}/lib/security/cacerts"; \
    chmod 644 "${JAVA_HOME}/lib/security/cacerts"; \
    test "$(keytool -list -cacerts -storepass changeit | grep -c 'trustedCertEntry')" -gt 100

RUN set -eux; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
        amd64) uv_arch=x86_64; uv_sha=@@UV_SHA256_AMD64@@ ;; \
        arm64) uv_arch=aarch64; uv_sha=@@UV_SHA256_ARM64@@ ;; \
        *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/uv.tar.gz \
        "https://github.com/astral-sh/uv/releases/download/@@UV_VERSION@@/uv-${uv_arch}-unknown-linux-musl.tar.gz"; \
    echo "${uv_sha}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp; \
    install -m 755 "/tmp/uv-${uv_arch}-unknown-linux-musl/uv" /usr/local/bin/uv; \
    install -m 755 "/tmp/uv-${uv_arch}-unknown-linux-musl/uvx" /usr/local/bin/uvx; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${uv_arch}-unknown-linux-musl"; \
    uv --version; \
    python3 --version | grep -q "Python @@PYTHON_VERSION@@\."; \
    java -version; \
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


class Unitycatalog2013To2013ImageBase(Image):
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
                "UV_VERSION": _UV_VERSION,
                "UV_SHA256_AMD64": _UV_SHA256_AMD64,
                "UV_SHA256_ARM64": _UV_SHA256_ARM64,
                "UV_EXCLUDE_NEWER": _UV_EXCLUDE_NEWER,
                "PYTHON_VERSION": _PYTHON_VERSION,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class Unitycatalog2013To2013ImageDefault(Image):
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
        return Unitycatalog2013To2013ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "SBT_FLAGS": _SBT_FLAGS,
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
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_NAME = re.compile(r'\bname="([^"]*)"')
_CLASSNAME = re.compile(r'\bclassname="([^"]*)"')
# JUnit Platform appends a parameter signature, e.g. "testFunctionCRUD()".
_SIGNATURE = re.compile(r"\([^()]*\)$")
_XML_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&"))


def _unescape(s: str) -> str:
    for k, v in _XML_ENTITIES:
        s = s.replace(k, v)
    return s


def _parse(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for m in _TESTCASE.finditer(clean):
        attrs = m.group(1)
        nm = _NAME.search(attrs)
        cn = _CLASSNAME.search(attrs)
        if not nm or not cn:
            continue
        name = _unescape(nm.group(1)).strip()
        cls = _unescape(cn.group(1)).strip()
        if not cls.startswith("tests."):
            name = _SIGNATURE.sub("", name)
        test_id = "%s#%s" % (cls, name)
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
class UNITYCATALOG_2013_TO_2013(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Unitycatalog2013To2013ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse(test_log)
