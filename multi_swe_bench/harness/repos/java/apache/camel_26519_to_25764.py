import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# apache/camel PRs 25764, 25811, 26519 (camel-4.18.x and camel-4.22.x maintenance
# branches). Every base sha pins <jdk.version>17</jdk.version> for the bytecode
# target, but the root pom's enforce-java-version rule requires JDK [21,) to BUILD
# (multi-release camel-util), so the shard toolchain is JDK 21. Maven comes from the
# repo's own ./mvnw (.mvn/wrapper/maven-wrapper.properties: 3.9.11 on 4.18.x, 3.9.16
# on 4.22.x), so each PR gets the Maven its own commit pins.
# The graded runs are OFFLINE (-o -nsu) after a non-tolerant online warm-up in
# prepare.sh that caches everything they need (incl. the surefire provider), so a
# graded run never resolves anything remotely at grade time.

_ORG = "apache"
_REPO = "camel"
_INTERVAL = "camel_26519_to_25764"

_BASE_TAG = f"base-{_INTERVAL}"

# Docker Hub library image via its ECR Public mirror (same multi-arch manifest,
# no anonymous Docker Hub rate limit).
_UBUNTU_IMAGE = "public.ecr.aws/docker/library/ubuntu:22.04"
_JDK_MAJOR = "21"
# p11-kit: regenerates the JDK truststore from the system CA bundle (see base
# Dockerfile). ca-certificates-java is NOT listed: the distro JRE already depends
# on it, and naming it explicitly makes apt pull default-jre-headless (JDK 11).
_BASE_APT = "git ca-certificates curl unzip openjdk-21-jdk-headless p11-kit"

# Closure (module dirs) of the reactor built by `-pl <test modules> -am` at prepare
# time. Lives outside the work tree so `git clean -fdx` cannot remove it.
_CLOSURE_FILE = "/home/camel_reactor_closure.txt"

_MVN_FLAGS = (
    "-B -ntp -nsu "
    "-Dmaven.test.failure.ignore=true "
    "-DfailIfNoTests=false "
    "-Dsurefire.failIfNoSpecifiedTests=false "
    "-Dsurefire.rerunFailingTestsCount=0 "
    "-Denforcer.skip=true "
    "-Dformat.skip=true "
    "-Dformatter.skip=true "
    "-Dimpsort.skip=true "
    "-Dlicense.skip=true "
    "-Djacoco.skip=true "
    "-Dcheckstyle.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dcyclonedx.skip=true "
    "-Ddevelocity.scan.disabled=true "
    "-Ddevelocity.cache.local.enabled=false "
    "-Ddevelocity.cache.remote.enabled=false"
)

# Extra flags for the prepare-time closure install only (Camel's own -Dquickly alias
# skips the non-essential "full" profile mojos; tests are not compiled-and-run there).
_INSTALL_FLAGS = "-DskipTests -Dquickly"

# Run before every graded/gate lifecycle so each selected module attaches its
# -sources jar to the reactor. camel-api-component-maven-plugin (e.g. camel-as2-component,
# fromApis) has a plugin dependency on the sibling *-api module's `sources` classifier
# and re-runs its generator whenever src/generated is newer than its target/ cache --
# which is exactly what applying a fix.patch that updates src/generated does (#25764).
# Without a reactor-attached sources jar the generator sees no .java files and fails
# ("No public non-static methods found"), so the whole component module never runs.
_SRC_JAR = "source:jar-no-fork"

_MVN_TIMEOUT = "timeout -k 60 10800"

# H1: third-party artifacts a fix.patch makes the graded fix stage need. Keyed by the
# parent/pom.xml version property the fix bumps; values are the groupId:artifactId
# pairs of the selected test modules that consume that property. The NEW version is
# parsed from fix.patch's added lines at render time (never applied in prepare.sh);
# prepare.sh only `dependency:get`s those coordinates (+ transitives) into ~/.m2 so
# the fix stage resolves them offline.
#   PR #26519: <bouncycastle-version> 1.83 -> 1.85; camel-pqc depends on
#   bcprov-jdk18on and (test scope) bcpkix-jdk18on (which pulls bcutil-jdk18on).
_FIX_BUMPED_PROPERTY_ARTIFACTS = {
    "bouncycastle-version": (
        "org.bouncycastle:bcprov-jdk18on",
        "org.bouncycastle:bcpkix-jdk18on",
    ),
}

_PROP_LINE_RE = re.compile(r"^([+-])\s*<([A-Za-z0-9_.-]+)>([A-Za-z0-9_.+-]+)</\2>\s*$")


def _fix_bumped_properties(fix_patch: str) -> dict:
    """{property: new value} for Maven properties a fix.patch changes in a pom.xml."""
    bumped: dict = {}
    removed: set = set()
    in_pom = False
    for line in fix_patch.splitlines():
        if line.startswith("diff --git "):
            in_pom = line.endswith("/pom.xml")
            continue
        if not in_pom or line.startswith(("+++", "---")):
            continue
        m = _PROP_LINE_RE.match(line)
        if not m:
            continue
        if m.group(1) == "-":
            removed.add(m.group(2))
        else:
            bumped[m.group(2)] = m.group(3)
    return {k: v for k, v in bumped.items() if k in removed}


def _fix_dep_prefetch(fix_patch: str) -> str:
    lines = []
    for prop, version in sorted(_fix_bumped_properties(fix_patch).items()):
        for ga in _FIX_BUMPED_PROPERTY_ARTIFACTS.get(prop, ()):
            lines.append(
                "@@MVN_TIMEOUT_PREPARE@@ ./mvnw @@MVN_FLAGS@@ -N dependency:get "
                '-Dtransitive=true -Dartifact="%s:%s"  # fix bumps <%s>'
                % (ga, version, prop)
            )
    if not lines:
        return "echo \"prepare.sh: fix.patch bumps no pre-fetchable dependency\""
    return "\n".join(lines)

_MVN_TIMEOUT_PREPARE = "timeout -k 60 14400"

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

# TEST_SEL  : Maven modules that own a src/test file touched by test.patch.
# CLASSES   : surefire classes (*Test/*Tests/*TestCase) touched by test.patch.
#             *IT classes are failsafe-only in Camel (parent pom excludes **/*IT.java
#             from surefire) and need live AWS/Vault containers, so they are never
#             selected; they are still compiled.
# SEL       : TEST_SEL plus every module the fix touches that is inside the reactor
#             closure prepare.sh built for TEST_SEL (so a fix in an upstream module,
#             e.g. camel-seda for camel-core tests, is rebuilt; generated catalog /
#             endpoint-DSL modules outside the closure are not).
_DERIVE_TARGETS = r"""CLASSES="$(sed -nE 's#^diff --git a/.* b/(.*/src/test/java/.*(Test|Tests|TestCase)\.java)$#\1#p' /home/test.patch \
    | sed -E 's#^.*/##; s#\.java$##' | sort -u | paste -sd, -)"
TEST_SEL=""
for _m in $(sed -nE 's#^diff --git a/.* b/(.*)/src/test/(java|resources)/.*$#\1#p' /home/test.patch | sort -u); do
    if [ -f "$_m/pom.xml" ]; then TEST_SEL="$TEST_SEL,$_m"; fi
done
TEST_SEL="${TEST_SEL#,}"
SEL="$TEST_SEL"
if [ -f @@CLOSURE_FILE@@ ]; then
    for _p in $(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/fix.patch | sort -u); do
        _d="$(dirname "$_p")"
        while [ "$_d" != "." ] && [ ! -f "$_d/pom.xml" ]; do _d="$(dirname "$_d")"; done
        [ "$_d" != "." ] || continue
        if grep -qxF "$_d" @@CLOSURE_FILE@@; then
            case ",$SEL," in *",$_d,"*) ;; *) SEL="$SEL,$_d" ;; esac
        fi
    done
fi
SEL="${SEL#,}"
"""

_RUN_TARGETS = r"""if [ -z "$TEST_SEL" ] || [ -z "$CLASSES" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi
echo "selected modules=[$SEL] classes=[$CLASSES]"

find . -type d -name surefire-reports -prune -exec rm -rf {} +

# Credited methods of a dropped (uncompilable) gold test class: the methods
# test.patch declares on an added line or that enclose a changed line, kept only
# when the patched source (copied before the drop) annotates them
# @Test/@ParameterizedTest/@RepeatedTest/@TestFactory/@TestTemplate, so helpers
# are never synthesized. Names are bare method names; parse_surefire_xml strips
# surefire's "(signature)" and folds "[N]" invocations to the same Class#method.
_annotated_tests() {
    awk '
        /^[ \t]*@([A-Za-z0-9_]+\.)*(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)([ \t(]|$)/ { ann = 1 }
        !ann { next }
        {
            r = $0
            while (match(r, /^[ \t]*@[A-Za-z0-9_.]+(\([^()]*\))?/)) r = substr(r, RSTART + RLENGTH)
            if (r ~ /^[ \t]*(@|\/\/|\/\*|\*|$)/) next
            if (match(r, /fun[ \t]+(`[^`]+`|[A-Za-z0-9_]+)[ \t]*\(/)) { m = substr(r, RSTART, RLENGTH); sub(/^fun[ \t]+/, "", m) }
            else if (match(r, /[A-Za-z0-9_>\]][ \t]+[A-Za-z0-9_]+[ \t]*\(/)) { m = substr(r, RSTART + 1, RLENGTH - 1) }
            else next
            sub(/^[ \t]+/, "", m); sub(/[ \t]*\($/, "", m); gsub(/`/, "", m)
            print m; ann = 0
        }
    ' "$1" | sort -u
}

_gold_methods() {
    comm -12 <(awk -v path="$1" '
        function decl(s,   m) {
            if (match(s, /fun[ \t]+(`[^`]+`|[A-Za-z0-9_]+)[ \t]*\(/)) {
                m = substr(s, RSTART, RLENGTH); sub(/^fun[ \t]+/, "", m)
            } else if (match(s, /^[ \t]*(@[A-Za-z0-9_.]+(\([^()]*\))?[ \t]+)*((public|protected|private|static|final|abstract|synchronized|default)[ \t]+)*[A-Za-z0-9_<>,.?\[\]]+[ \t]+[A-Za-z0-9_]+[ \t]*\(/)) {
                m = substr(s, RSTART, RLENGTH)
                if (m ~ /^[ \t]*(return|new|throw|else|case|assert|yield)[ \t]/) return ""
                sub(/[ \t]*\($/, "", m); sub(/.*[ \t]/, "", m)
            } else return ""
            sub(/[ \t]*\($/, "", m); gsub(/`/, "", m)
            return m
        }
        index($0, "diff --git ") == 1 { inf = ($NF == "b/" path); inh = 0; cur = ""; next }
        !inf { next }
        index($0, "@@") == 1 { inh = 1; h = $0; sub(/^@@[^@]*@@[ \t]*/, "", h); cur = decl(h); next }
        !inh { next }
        /^[-+ ]/ {
            m = decl(substr($0, 2)); if (m != "") cur = m
            if (index($0, " ") != 1 && cur != "") print cur
        }
    ' /home/test.patch | sort -u) <(_annotated_tests "$2")
}

_DROP_DIR="$(mktemp -d)"
DROPPED=""
for _attempt in 1 2 3 4 5; do
    if _out="$(@@MVN_TIMEOUT@@ ./mvnw -o @@MVN_FLAGS@@ @@SRC_JAR@@ test-compile -pl "$SEL" 2>&1)"; then
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
    while IFS= read -r _l; do
        [ -n "$_l" ] || continue
        _r="${_l#@@REPO_DIR@@/}"
        cp -f "$_l" "$_DROP_DIR/$(printf '%s' "$_r" | tr '/' '_')"
        rm -f "$_l"
    done <<< "$_bad"
done

MVN_RC=0
@@MVN_TIMEOUT@@ ./mvnw -o @@MVN_FLAGS@@ @@SRC_JAR@@ test -pl "$SEL" -Dtest="$CLASSES" || MVN_RC=$?

find . -path '*/surefire-reports/TEST-*.xml' -exec cat {} +

for _f in $DROPPED; do
    _cls="$(basename "$_f" .java)"
    case ",$CLASSES," in
        *",$_cls,"*) ;;
        *) continue ;;
    esac
    _pkg="$(printf '%s' "$_f" | sed -E 's#^.*/src/test/java/##; s#/[^/]*$##; s#/#.#g')"
    _rel="${_f#@@REPO_DIR@@/}"
    _methods="$(_gold_methods "$_rel" "$_DROP_DIR/$(printf '%s' "$_rel" | tr '/' '_')")"
    if [ -z "$_methods" ]; then
        echo "SYNTHETIC_FAILURES_SKIPPED $_pkg.$_cls (no patch-touched method found)"
        continue
    fi
    echo "SYNTHESIZED_COMPILE_FAILURES $_pkg.$_cls"
    echo "<testsuite name=\"$_pkg.$_cls\">"
    while IFS= read -r _m; do
        [ -n "$_m" ] || continue
        _m="$(printf '%s' "$_m" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g')"
        echo "  <testcase name=\"$_m\" classname=\"$_pkg.$_cls\"><failure type=\"CompilationFailure\">gold test source did not compile at this stage</failure></testcase>"
    done <<< "$_methods"
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

# ---------- Section 2: PROVISION (build time, pinned by this commit's poms + mvnw) ----------
rm -f @@CLOSURE_FILE@@
@@DERIVE_TARGETS@@
echo "prepare.sh: test modules=[$TEST_SEL] classes=[$CLASSES]"
test -n "$TEST_SEL"

# Install the -am closure of the test modules into ~/.m2 (the whole 600-module
# reactor is far too large to build per stage). The graded runs then build only
# the selected modules against these era-pinned artifacts. buildingtools
# (camel-buildtools) is added explicitly: it is not a Maven dependency of anything,
# but the "full" profile (active without -Dquickly, i.e. in every graded run, since
# -Dquickly also unbinds surefire) declares it as a formatter-maven-plugin
# dependency, and plugin dependencies are resolved even when the goal is skipped.
_log=/tmp/reactor_install.log
if ! @@MVN_TIMEOUT_PREPARE@@ ./mvnw @@MVN_FLAGS@@ install @@INSTALL_FLAGS@@ -pl "buildingtools,$TEST_SEL" -am > "$_log" 2>&1; then
    grep -E '^\[ERROR\]' "$_log" | head -60
    tail -40 "$_log"
    exit 1
fi
grep -E '^\[INFO\] (BUILD|Total time)' "$_log"
sed -nE \
    -e 's#^\[INFO\] Installing @@REPO_DIR@@/(.+)/target/[^/ ]+\.pom to .*#\1#p' \
    -e 's#^\[INFO\] Installing @@REPO_DIR@@/(.+)/[^/ ]*pom\.xml to .*#\1#p' \
    "$_log" | sort -u > @@CLOSURE_FILE@@
test -s @@CLOSURE_FILE@@
rm -f "$_log"
echo "prepare.sh: reactor closure has $(wc -l < @@CLOSURE_FILE@@) modules"

# Pre-fetch third-party artifacts at the versions the fix declares (see
# _FIX_BUMPED_PROPERTY_ARTIFACTS): third-party jars only into ~/.m2; the tree stays
# at the base sha and fix.patch is not applied.
@@FIX_DEPS@@

# Warm-up (ONLINE, non-tolerant): run the selected classes once so everything the
# OFFLINE graded `test` runs need is cached -- notably the surefire JUnit provider,
# which surefire resolves only at `test` time, never at `test-compile`. Test
# failures are absorbed by -Dmaven.test.failure.ignore=true, so a non-zero exit
# here is a build/resolution error and must fail the image build.
@@DERIVE_TARGETS@@
# A -Dtest that matches no class never makes surefire fork, so it would never
# resolve the provider: warm up with the gold classes that already exist at the
# base commit, plus one existing *Test class for any test module where none does
# (classes that test.patch adds are absent here).
_tracked="$(git ls-files -- '*/src/test/*')"
WARM=""
for _m in $(printf '%s' "$TEST_SEL" | tr ',' ' '); do
    _hit=""
    for _c in $(printf '%s' "$CLASSES" | tr ',' ' '); do
        if [ -n "$(awk -v re="^$_m/src/test/(java|kotlin)/(.*/)?$_c[.](java|kt)\$" '$0 ~ re { print; exit }' <<< "$_tracked")" ]; then
            _hit=1
            case ",$WARM," in *",$_c,"*) ;; *) WARM="$WARM,$_c" ;; esac
        fi
    done
    if [ -z "$_hit" ]; then
        _any="$(awk -v re="^$_m/src/test/(java|kotlin)/.*/[A-Za-z0-9_]+(Test|Tests)[.](java|kt)\$" '$0 ~ re { print; exit }' <<< "$_tracked" | sed -E 's#^.*/##; s#[.](java|kt)$##')"
        if [ -n "$_any" ]; then WARM="$WARM,$_any"; fi
    fi
done
WARM="${WARM#,}"
echo "prepare.sh: warm-up classes=[$WARM]"
if [ -n "$WARM" ]; then
    _wlog=/tmp/warmup.log
    if ! @@MVN_TIMEOUT_PREPARE@@ ./mvnw @@MVN_FLAGS@@ @@SRC_JAR@@ test -pl "$SEL" -Dtest="$WARM" > "$_wlog" 2>&1; then
        grep -E '^\[ERROR\]' "$_wlog" | head -60
        tail -40 "$_wlog"
        exit 1
    fi
    awk '/^\[INFO\] (BUILD|Total time|Tests run:)/' "$_wlog" | tail -5
    _sfv="$(sed -nE 's#^\[INFO\] --- (maven-)?surefire(-plugin)?:([0-9][^: ]*):test .*#\3#p' "$_wlog" | sort -u | paste -sd' ' -)"
    rm -f "$_wlog"
fi
find . -type d -name surefire-reports -prune -exec rm -rf {} +

# ---------- Section 3: HARD GATE (non-tolerant, offline) ----------
for _m in $(printf '%s' "$TEST_SEL" | tr ',' ' '); do
    grep -qxF "$_m" @@CLOSURE_FILE@@
done
@@MVN_TIMEOUT_PREPARE@@ ./mvnw -o @@MVN_FLAGS@@ @@SRC_JAR@@ test-compile -pl "$SEL"
# The OFFLINE graded `test` runs need the provider of every surefire version the
# warm-up ran (surefire resolves it only at `test` time, never at test-compile).
test -n "$_sfv"
for _v in $_sfv; do
    _ok=""
    for _p in junit-platform junit4 junit47 testng; do
        if compgen -G "/root/.m2/repository/org/apache/maven/surefire/surefire-$_p/$_v/surefire-$_p-$_v.jar" > /dev/null; then _ok=1; fi
    done
    test -n "$_ok"
done
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

RUN mkdir -p /usr/share/binfmts && \
    apt-get update && apt-get install -y --no-install-recommends \
    @@APT@@ \
    && rm -rf /var/lib/apt/lists/*

RUN ln -s "/usr/lib/jvm/java-@@JDK@@-openjdk-$(dpkg --print-architecture)" /usr/lib/jvm/java-@@JDK@@-openjdk

ENV JAVA_HOME=/usr/lib/jvm/java-@@JDK@@-openjdk \
    MAVEN_OPTS="-Xmx3g"

# The JDK must trust exactly what is in the system bundle (${CA_CERT_PATH}), which
# may carry a build-time proxy CA: regenerate the JDK truststore from it with
# p11-kit (its Debian trust module reads /etc/ssl/certs/ca-certificates.crt) --
# fast and arch-independent, unlike a per-cert keytool loop under qemu. The distro
# JDK's cacerts is a symlink to ca-certificates-java's /etc/ssl/certs/java/cacerts,
# so the extract writes through to the file the JDK actually loads.
RUN set -eux; \
    _c="${JAVA_HOME}/lib/security/cacerts"; \
    [ -e "$_c" ] || _c="${JAVA_HOME}/jre/lib/security/cacerts"; \
    dpkg -s ca-certificates-java >/dev/null; \
    test "$(readlink -f "$_c")" = /etc/ssl/certs/java/cacerts; \
    _t="$(readlink -f "$_c")"; \
    p11-kit extract --format=java-cacerts --purpose=server-auth --overwrite "$_t"; \
    test "$(readlink -f "$_c")" = "$_t"; \
    _n="$("${JAVA_HOME}/bin/keytool" -list -keystore "$_c" -storepass changeit | grep -c 'trustedCertEntry')"; \
    echo "JDK truststore entries: $_n"; \
    test "$_n" -gt 100

RUN java -version && javac -version

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


class Camel26519To25764ImageBase(Image):
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
        return _UBUNTU_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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
                "JDK": _JDK_MAJOR,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class Camel26519To25764ImageDefault(Image):
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
        return Camel26519To25764ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "MVN_FLAGS": _MVN_FLAGS,
            "MVN_TIMEOUT_PREPARE": _MVN_TIMEOUT_PREPARE,
            "MVN_TIMEOUT": _MVN_TIMEOUT,
            "CLOSURE_FILE": _CLOSURE_FILE,
            "INSTALL_FLAGS": _INSTALL_FLAGS,
            "SRC_JAR": _SRC_JAR,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "APPLY_FN": _APPLY_FN,
                "DERIVE_TARGETS": _DERIVE_TARGETS,
                "RUN_TARGETS": _RUN_TARGETS,
                "FIX_DEPS": _fix_dep_prefetch(self.pr.fix_patch),
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
# Surefire's JUnit Platform provider reports "method(Type, Type)" and, for
# @ParameterizedTest/@RepeatedTest/@TestFactory invocations, "method(Type)[N]".
# Strip the signature and fold every invocation into one Class#method (FAIL if any
# invocation fails, else PASS if any passes, else SKIP), so the real result and a
# compile-failure stub (bare method name) carry the same name in every stage.
_SIG_SUFFIX = re.compile(r"(\([^()]*\))?(\[[^\]]*\])?$")
_RANK = {"skip": 0, "pass": 1, "fail": 2}


def parse_surefire_xml(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log)

    status: dict[str, str] = {}
    for m in _TESTCASE.finditer(clean):
        attrs = m.group(1)
        nm = _NAME.search(attrs)
        cn = _CLASSNAME.search(attrs)
        if not nm or not cn:
            continue
        name = _SIG_SUFFIX.sub("", nm.group(1).strip(), count=1).strip()
        if not name:
            continue
        test_id = "%s#%s" % (cn.group(1).strip(), name)
        inner = m.group(3) or ""
        if m.group(2) == "/>":
            st = "pass"
        elif "<failure" in inner or "<error" in inner:
            st = "fail"
        elif "<skipped" in inner:
            st = "skip"
        else:
            st = "pass"
        prev = status.get(test_id)
        if prev is None or _RANK[st] > _RANK[prev]:
            status[test_id] = st

    passed = {t for t, s in status.items() if s == "pass"}
    failed = {t for t, s in status.items() if s == "fail"}
    skipped = {t for t, s in status.items() if s == "skip"}

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
class CAMEL_26519_TO_25764(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Camel26519To25764ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_surefire_xml(test_log)
