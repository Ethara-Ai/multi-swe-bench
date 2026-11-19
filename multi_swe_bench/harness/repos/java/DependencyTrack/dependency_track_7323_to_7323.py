import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "DependencyTrack"
_REPO = "dependency-track"
_INTERVAL = "dependency_track_7323_to_7323"

# JDK: pom.xml compiles with <release>25</release> and the enforcer requires
# Java [25,); .github/workflows/ci-test.yaml uses Temurin 25.
# The Docker official image is referenced through its ECR Public mirror
# (same multi-arch manifest as docker.io/library/eclipse-temurin) to avoid
# Docker Hub's anonymous pull rate limit.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:25.0.4_7-jdk-noble"

# Build tool: no wrapper is checked in; the enforcer requires Maven [3.9,)
# and CI uses the runner's Maven 3.9.x, so the 3.9.11 release is installed.
_MAVEN_VERSION = "3.9.11"
_MAVEN_SHA512 = (
    "bcfe4fe305c962ace56ac7b5fc7a08b87d5abd8b7e89027ab251069faebee516"
    "b0ded8961445d6d91ec1985dfe30f8153268843c89aa392733d1a3ec956c9978"
)

_BASE_APT = "git ca-certificates curl p11-kit"

# -T 2 overrides .mvn/maven.config's --threads=1C. The maven-build-cache
# extension (.mvn/extensions.xml) is disabled so every stage really compiles
# and runs the tests (Makefile's MVN_NO_BUILDCACHE). The remaining skips match
# `make test` (-Dspotless.check.skip -Dcyclonedx.skip) plus coverage/enforcer.
# Test classes using Testcontainers cannot run without a docker socket; only
# the classes test.patch touches are selected (WireMock, in-process).
_MVN_FLAGS = (
    "-B -ntp -T 2 "
    "-Dmaven.build.cache.enabled=false "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dspotless.check.skip=true "
    "-Dspotless.apply.skip=true "
    "-Dcyclonedx.skip=true "
    "-Djacoco.skip=true "
    "-Denforcer.skip=true "
    "-Dmaven.javadoc.skip=true"
)

_MVN_TIMEOUT = "timeout -k 60 2700"
_MVN_TIMEOUT_PREPARE = "timeout -k 60 3600"

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

# Test selection: only the test classes test.patch touches, in the Maven
# modules that own them (plus their in-repo upstream modules via -am).
_DERIVE_TARGETS = r"""CLASSES="$(sed -nE 's#^diff --git a/.* b/(.*/src/test/java/.*(Test|Tests|TestCase)\.java)$#\1#p' /home/test.patch \
    | sed -E 's#^.*/##; s#\.java$##' | sort -u | paste -sd, -)"
MODULES="$(sed -nE 's#^diff --git a/.* b/(.*)/src/test/(java|resources)/.*$#\1#p' /home/test.patch | sort -u)"
SEL=""
for _m in $MODULES; do
    if [ -f "$_m/pom.xml" ]; then SEL="$SEL,$_m"; fi
done
SEL="${SEL#,}"
"""

# When test.patch references API that only fix.patch adds, javac rejects the
# whole test source set. Uncompilable test files are removed (iteratively,
# from the compiler's own error list) so the rest of the module still runs,
# and the @Test methods test.patch adds/changes in a dropped class are
# reported as failures so the instance records the pre-fix state honestly.
_RUN_TARGETS = r"""if [ -z "$SEL" ] || [ -z "$CLASSES" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi

find . -type d -name surefire-reports -prune -exec rm -rf {} +

_gold_methods() {
    awk -v tag="/$1.java b/" '
        function mname(s,   m) {
            if (match(s, /^[ \t]*((public|protected)[ \t]+)?(static[ \t]+)?void[ \t]+[A-Za-z0-9_]+[ \t]*\(/)) {
                m = substr(s, RSTART, RLENGTH)
                sub(/.*void[ \t]+/, "", m)
                sub(/[ \t]*\(.*/, "", m)
                return m
            }
            return ""
        }
        function flush(   i) {
            if (nadd > 0) { for (i = 1; i <= nadd; i++) print addl[i] }
            else if (chg && ctx != "") { print ctx }
            nadd = 0; ctx = ""; chg = 0; ann = 0
        }
        index($0, "diff --git ") == 1 { flush(); inf = (index($0, tag) > 0); next }
        !inf { next }
        index($0, "@@") == 1 { flush(); h = $0; sub(/^@@[^@]*@@[ \t]*/, "", h); ctx = mname(h); next }
        /^[-+ ]/ {
            l = substr($0, 2)
            if (index($0, " ") != 1) { chg = 1 }
            if (l ~ /@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)([ \t(]|$)/) { ann = 1; next }
            m = mname(l)
            if (m != "") {
                if (ann && index($0, "+") == 1) { addl[++nadd] = m }
                else if (ann) { ctx = m; chg = 0 }
                else { ctx = ""; chg = 0 }
                ann = 0
            }
        }
        END { flush() }
    ' /home/test.patch | sort -u
}

DROPPED=""
for _attempt in 1 2 3 4 5; do
    if _out="$(@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ test-compile -pl "$SEL" -am 2>&1)"; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" \
        | sed -nE 's#^\[ERROR\] (@@REPO_DIR@@/[^:]*/src/test/java/[^:]*\.java):\[[0-9]+,[0-9]+\].*#\1#p' \
        | sort -u)"
    if [ -z "$_bad" ]; then
        awk '/^\[ERROR\]/ && n++ < 40' <<< "$_out"
        break
    fi
    echo "DROPPED_UNCOMPILABLE_TEST_SOURCES (attempt $_attempt):"
    printf '%s\n' "$_bad" | sed 's/^/  /'
    while IFS= read -r _l; do
        [ -n "$_l" ] || continue
        echo "COMPILE_ERRORS_FOR $_l"
        awk -v pat="$_l" 'index($0, pat) && n++ < 20 { print "    " $0 }' <<< "$_out"
    done <<< "$_bad"
    DROPPED="$DROPPED $_bad"
    printf '%s\n' "$_bad" | xargs -r rm -f
done

MVN_RC=0
@@MVN_TIMEOUT@@ mvn @@MVN_FLAGS@@ test -pl "$SEL" -am -Dtest="$CLASSES" || MVN_RC=$?

find . -path '*/surefire-reports/TEST-*.xml' -exec cat {} +

for _f in $DROPPED; do
    _cls="$(basename "$_f" .java)"
    case ",$CLASSES," in
        *",$_cls,"*) ;;
        *) continue ;;
    esac
    _pkg="$(printf '%s' "$_f" | sed -E 's#^.*/src/test/java/##; s#/[^/]*$##; s#/#.#g')"
    _methods="$(_gold_methods "$_cls")"
    if [ -z "$_methods" ]; then
        echo "SYNTHETIC_FAILURES_SKIPPED $_pkg.$_cls (no patch-touched test method found)"
        continue
    fi
    echo "SYNTHESIZED_COMPILE_FAILURES $_pkg.$_cls"
    echo "<testsuite name=\"$_pkg.$_cls\">"
    for _m in $_methods; do
        echo "  <testcase name=\"$_m\" classname=\"$_pkg.$_cls\"><failure type=\"CompilationFailure\">test source did not compile at this stage</failure></testcase>"
    done
    echo "</testsuite>"
done

if [ "$MVN_RC" -ne 0 ]; then
    echo "MVN_EXIT_CODE=$MVN_RC"
    exit "$MVN_RC"
fi
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

# ---------- Section 2: PROVISION (build time, pinned by this commit's poms) ----------
@@DERIVE_TARGETS@@

echo "prepare.sh: modules=[$SEL] classes=[$CLASSES]"
# Warm-up run resolves every plugin, dependency and the surefire JUnit
# provider into ~/.m2. Its test outcome is irrelevant (the selected classes
# may not exist yet) and is absorbed by -Dmaven.test.failure.ignore and
# -Dsurefire.failIfNoSpecifiedTests=false, so any non-zero exit here is a real
# build or download failure and aborts the build (non-tolerant). The offline gate below re-checks
# that resolution actually succeeded.
if [ -n "$SEL" ] && [ -n "$CLASSES" ]; then
    @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ test -pl "$SEL" -am -Dtest="$CLASSES"
else
    @@MVN_TIMEOUT_PREPARE@@ mvn @@MVN_FLAGS@@ -N validate
fi

git clean -fdx
find . -type d -name target -prune -exec rm -rf {} +

# ---------- Section 3: HARD GATE (offline, non-tolerant) ----------
if [ -n "$SEL" ]; then
    @@MVN_TIMEOUT_PREPARE@@ mvn -o @@MVN_FLAGS@@ test-compile -pl "$SEL" -am
else
    @@MVN_TIMEOUT_PREPARE@@ mvn -o @@MVN_FLAGS@@ -N validate
fi

find . -type d -name target -prune -exec rm -rf {} +
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

RUN apt-get update && apt-get install -y --no-install-recommends \
    @@APT@@ \
    && rm -rf /var/lib/apt/lists/*

# The Temurin JDK ships its own cacerts and ignores the system bundle. Rebuild
# it from /etc/ssl/certs/ca-certificates.crt (CA_CERT_PATH, where a build-time
# proxy CA lands) via p11-kit's trust module, so Maven/Gradle trust exactly
# what curl/git trust. Fast and arch-independent (no per-cert keytool loop).
RUN p11-kit extract --format=java-cacerts --purpose=server-auth --overwrite \
        "$JAVA_HOME/lib/security/cacerts" && \
    n="$(keytool -list -cacerts -storepass changeit | grep -c trustedCertEntry)" && \
    echo "JDK cacerts trusted entries: $n" && test "$n" -gt 100

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

RUN git config --global --add safe.directory '*'

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


class DependencyTrack7323To7323ImageBase(Image):
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
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class DependencyTrack7323To7323ImageDefault(Image):
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
        return DependencyTrack7323To7323ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "MVN_FLAGS": _MVN_FLAGS,
            "MVN_TIMEOUT_PREPARE": _MVN_TIMEOUT_PREPARE,
            "MVN_TIMEOUT": _MVN_TIMEOUT,
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


_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_NAME = re.compile(r'\bname="([^"]*)"')
_CLASSNAME = re.compile(r'\bclassname="([^"]*)"')
# Surefire's JUnit Platform provider appends the parameter signature, e.g.
# "shouldEncodeFile(File)" or "parse(String)[2]". Drop the signature (keep the
# invocation index) so a compile-failure stub and the real result share a name.
_SIGNATURE = re.compile(r"\([^()]*\)(?=(\[[^\]]*\])?$)")


def parse_surefire_xml(test_log: str) -> TestResult:
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
        test_id = "%s#%s" % (cn.group(1), _SIGNATURE.sub("", nm.group(1).strip()))
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
class DEPENDENCY_TRACK_7323_TO_7323(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DependencyTrack7323To7323ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_xml(test_log)
