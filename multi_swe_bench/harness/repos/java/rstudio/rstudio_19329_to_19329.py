import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "rstudio"
_REPO = "rstudio"
_INTERVAL = "rstudio_19329_to_19329"

# Credited tests: GWT JUnit (GWTTestCase) classes under src/gwt/test, run the
# way src/gwt/build.xml's `unittest` target runs them (JUnitCore, classpath =
# test + src + bin + GWT SDK jars, -Dgwt.args=-sourceLevel 17; GWT's JUnitShell
# drives the bundled HtmlUnit, so no real browser is needed). The Playwright
# e2e spec in test.patch needs a full RStudio Server/Desktop build and is not
# run.
#
# JDK: build.xml sets javac.version=17 (javac --source/--target 17 and GWT
# -sourceLevel 17). The Docker official image is referenced through its ECR
# Public mirror (same multi-arch manifest as docker.io/library/eclipse-temurin)
# to avoid Docker Hub's anonymous pull rate limit.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:17.0.20_8-jdk-jammy"

# GWT SDK: dependencies/common/install-gwt pins RStudio's GWT fork build
# GWT_VERSION=2.13.1-montauk-daisy-4 (tarball also carries gin, elemental2,
# jsinterop, jspecify and junit-4.9b3). It is architecture independent and is
# unpacked where build.xml looks for it (/opt/rstudio-tools/...).
_GWT_VERSION = "2.13.1-montauk-daisy-4"
_GWT_SHA256 = "ca7a8469a866fe193d4f51709fbc0a7a2a416b0850d6cae1aaa9f7838786b518"
_GWT_LIB_DIR = "/opt/rstudio-tools/dependencies/common/gwtproject"

# ant: build.xml is an Ant project (Ubuntu jammy ships Ant 1.10.12).
_BASE_APT = "git ca-certificates p11-kit curl ant"

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

# JUnit 4 runner that prints one stable line per test case (JUnitCore's own
# text output only lists failures).
_RUNNER_JAVA = r"""import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import org.junit.runner.Description;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.notification.Failure;
import org.junit.runner.notification.RunListener;

public class MswbTestRunner
{
   private static String id(Description d)
   {
      String m = d.getMethodName();
      return d.getClassName() + (m == null ? "" : "#" + m);
   }

   public static void main(String[] args) throws Exception
   {
      final Set<String> failed = new HashSet<String>();
      JUnitCore core = new JUnitCore();
      core.addListener(new RunListener()
      {
         @Override
         public void testFailure(Failure f)
         {
            String n = id(f.getDescription());
            failed.add(n);
            System.out.println("TESTCASE FAILED " + n);
            String t = f.getTrace();
            if (t != null)
               System.out.println(t.length() > 4000 ? t.substring(0, 4000) : t);
         }

         @Override
         public void testAssumptionFailure(Failure f)
         {
            testFailure(f);
         }

         @Override
         public void testIgnored(Description d)
         {
            System.out.println("TESTCASE SKIPPED " + id(d));
         }

         @Override
         public void testFinished(Description d)
         {
            String n = id(d);
            if (!failed.contains(n))
               System.out.println("TESTCASE PASSED " + n);
         }
      });
      List<Class<?>> classes = new ArrayList<Class<?>>();
      for (String a : args)
         classes.add(Class.forName(a));
      core.run(Request.classes(classes.toArray(new Class<?>[0])));
      System.exit(0);
   }
}
"""

_APPLY_FN = r"""_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}
"""

# Test selection: the GWTTestCase classes test.patch touches that exist at
# this stage (suite aggregators such as RStudioUnitTestSuite are skipped: they
# re-run the whole suite). Paths are relative to src/gwt.
_DERIVE_TARGETS = r"""TEST_FILES=""
for _f in $(sed -nE 's#^diff --git a/.* b/(src/gwt/test/.*\.java)$#\1#p' /home/test.patch | sort -u); do
    [ -f "@@REPO_DIR@@/$_f" ] || continue
    grep -qE "extends[[:space:]]+GWTTestCase" "@@REPO_DIR@@/$_f" || continue
    TEST_FILES="$TEST_FILES ${_f#src/gwt/}"
done
echo "selected GWT test classes: [${TEST_FILES# }]"
"""

# Main sources are (re)compiled with build.xml's own targets (acesupport +
# javac). The selected test classes are compiled with javac against them; a
# test file that does not compile at this stage (it references API only the
# fix adds) is dropped, and its test methods are reported as failures.
_RUN_TARGETS = r"""if [ -z "$TEST_FILES" ]; then
    echo "NO_TESTS_AT_THIS_STAGE"
    exit 0
fi

cd @@REPO_DIR@@/src/gwt
@@TIMEOUT@@ ant -noinput acesupport javac

CP="bin:$(find @@GWT_LIB_DIR@@ -name '*.jar' ! -name '*-sources.jar' ! -name '*src.jar' | sort | paste -sd: -)"
rm -rf /tmp/gwt-test-classes
mkdir -p /tmp/gwt-test-classes

KEEP="$TEST_FILES"
DROPPED=""
for _attempt in 1 2 3 4 5; do
    [ -n "$KEEP" ] || break
    if _out="$(javac -nowarn -encoding utf-8 --release 17 -proc:none -d /tmp/gwt-test-classes \
            -cp "$CP" -sourcepath test $KEEP 2>&1)"; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" | sed -nE 's#^(test/[^:]+\.java):[0-9]+: error:.*#\1#p' | sort -u)"
    if [ -z "$_bad" ]; then
        awk 'NR <= 40' <<< "$_out"
        echo "TEST_COMPILE_FAILED"
        exit 1
    fi
    echo "DROPPED_UNCOMPILABLE_TEST_SOURCES (attempt $_attempt):"
    awk '/^test\/.*: error:/ && n++ < 20' <<< "$_out"
    DROPPED="$DROPPED $_bad"
    _next=""
    for _k in $KEEP; do
        case " $_bad " in *" $_k "*) ;; *) _next="$_next $_k" ;; esac
    done
    KEEP="$_next"
done

CLASSES=""
for _k in $KEEP; do
    CLASSES="$CLASSES $(printf '%s' "$_k" | sed -E 's#^test/##; s#\.java$##; s#/#.#g')"
done

RC=0
if [ -n "$CLASSES" ]; then
    rm -rf www-test gwt-unitCache
    @@TIMEOUT@@ java -Xmx3g -Xss16M \
        -Dgwt.args="-sourceLevel 17" -Djava.awt.headless=true \
        -cp "/home/mswb-runner:/tmp/gwt-test-classes:test:src:$CP" \
        MswbTestRunner $CLASSES || RC=$?
fi

for _f in $DROPPED; do
    _cls="$(printf '%s' "$_f" | sed -E 's#^test/##; s#\.java$##; s#/#.#g')"
    for _m in $(sed -nE 's#^[[:space:]]*public[[:space:]]+void[[:space:]]+(test[A-Za-z0-9_]*)[[:space:]]*\(.*#\1#p' "$_f" | sort -u); do
        echo "TESTCASE FAILED $_cls#$_m"
    done
done

cd @@REPO_DIR@@
echo "TEST_RUNNER_EXIT_CODE=$RC"
# Propagate the runner's status (after the synthesized FAILED lines above) so a
# crashed / timed-out JVM fails the stage instead of looking like a clean run.
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

# ---------- Section 2: PROVISION (build time) ----------
# Compile the test runner helper (outside the repo) against the SDK's junit.
mkdir -p /home/mswb-runner
javac --release 17 -d /home/mswb-runner -cp "@@GWT_LIB_DIR@@/junit-4.9b3.jar" /home/MswbTestRunner.java

# Main GWT sources compiled once via build.xml (acesupport + javac); the run
# scripts recompile incrementally after applying patches.
cd @@REPO_DIR@@/src/gwt
@@TIMEOUT_PREPARE@@ ant -noinput acesupport javac

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# The whole GWT unit-test source set must compile (build.xml build-unittests),
# and GWT's JUnitShell must actually run a known-good GWTTestCase in HtmlUnit.
@@TIMEOUT_PREPARE@@ ant -noinput build-unittests
CP="bin:$(find @@GWT_LIB_DIR@@ -name '*.jar' ! -name '*-sources.jar' ! -name '*src.jar' | sort | paste -sd: -)"
_gate="$(@@TIMEOUT_PREPARE@@ java -Xmx3g -Xss16M -Dgwt.args="-sourceLevel 17" -Djava.awt.headless=true \
    -cp "/home/mswb-runner:test:src:$CP" MswbTestRunner org.rstudio.core.client.StringUtilTests)"
grep -q '^TESTCASE PASSED org.rstudio.core.client.StringUtilTests#' <<< "$_gate"
if grep -q '^TESTCASE FAILED' <<< "$_gate"; then
    awk '/^TESTCASE FAILED/ {k = 6} k > 0 && n++ < 40 {print; k--}' <<< "$_gate"
    exit 1
fi
rm -rf www-test gwt-unitCache
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
    ANT_OPTS="-Xmx3g"
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
# JDK's own truststore, which ant and the JVM use instead of /etc/ssl/certs.
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
    mkdir -p @@GWT_LIB_DIR@@; \
    curl -fsSL -o /tmp/gwt.tar.gz \
        "https://rstudio-buildtools.s3.us-east-1.amazonaws.com/gwt/gwt-@@GWT_VERSION@@.tar.gz"; \
    echo "@@GWT_SHA256@@  /tmp/gwt.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/gwt.tar.gz -C @@GWT_LIB_DIR@@ --no-same-owner 2>/dev/null; \
    rm -f /tmp/gwt.tar.gz; \
    touch @@GWT_LIB_DIR@@/gwt-@@GWT_VERSION@@; \
    test -f @@GWT_LIB_DIR@@/gwt/gwt-rstudio/gwt-dev.jar; \
    test -f @@GWT_LIB_DIR@@/junit-4.9b3.jar; \
    java -version; \
    ant -version; \
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


class Rstudio19329To19329ImageBase(Image):
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
                "GWT_VERSION": _GWT_VERSION,
                "GWT_SHA256": _GWT_SHA256,
                "GWT_LIB_DIR": _GWT_LIB_DIR,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class Rstudio19329To19329ImageDefault(Image):
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
        return Rstudio19329To19329ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "GWT_LIB_DIR": _GWT_LIB_DIR,
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
            File(".", "MswbTestRunner.java", _RUNNER_JAVA),
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
_CASE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (\S+)\s*$")


def _parse(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log).replace("\r\n", "\n")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for line in clean.split("\n"):
        m = _CASE.match(line.strip())
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "FAILED":
            failed.add(name)
        elif status == "SKIPPED":
            skipped.add(name)
        else:
            passed.add(name)

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
class RSTUDIO_19329_TO_19329(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Rstudio19329To19329ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse(test_log)
