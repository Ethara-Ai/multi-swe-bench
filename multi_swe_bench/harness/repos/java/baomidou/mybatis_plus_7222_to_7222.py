import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "baomidou"
_REPO = "mybatis-plus"
_INTERVAL = "mybatis_plus_7222_to_7222"

# JDK: build.gradle sets `javaVersion = JavaVersion.VERSION_21` as the
# sourceCompatibility of every subproject (CI matrix 17/21/25 cannot build with
# 17 because of it), and the Gradle 9 wrapper needs JDK 17+ to run -> JDK 21.
# The Docker official image is referenced through its ECR Public mirror
# (same multi-arch manifest as docker.io/library/eclipse-temurin) to avoid
# Docker Hub's anonymous pull rate limit.
_JDK_IMAGE = "public.ecr.aws/docker/library/eclipse-temurin:21.0.12_8-jdk-noble"

# Build tool: the checked-in ./gradlew; gradle/wrapper/gradle-wrapper.properties
# pins gradle-9.8.2-bin.zip (pure JVM distribution, arch-independent). It is
# downloaded by prepare.sh because the wrapper pin belongs to the commit.
_BASE_APT = "git ca-certificates curl unzip p11-kit"

_GRADLE = (
    "./gradlew --no-daemon --console=plain --max-workers=2 --continue "
    "-Dorg.gradle.parallel=false "
    "-Dorg.gradle.jvmargs=-Xmx3g "
    "-Dorg.gradle.caching=false"
)

_GRADLE_TIMEOUT = "timeout -k 60 2700"
_GRADLE_TIMEOUT_PREPARE = "timeout -k 60 3600"

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

# Resolves each selected project's testRuntimeClasspath (jars, not only the
# graph) so the offline gate can prove the test runtime is cached. Written to
# /home, outside the tree; only prepare.sh uses it.
_INIT_GRADLE = r"""allprojects {
    pluginManager.withPlugin('java') {
        def cp = configurations.named('testRuntimeClasspath')
        tasks.register('mswbResolveTestDeps') {
            inputs.files(cp)
            doLast { println "mswbResolveTestDeps ${path}: ${cp.get().files.size()} files" }
        }
        // -Pmswb.fixDeps=":proj=g:a:v;...": third-party coordinates the PR's
        // fix.patch adds to a project's build script. Each is resolved (with
        // its transitive deps, conflict-resolved against the project's own
        // compile/test classpaths) through a detached copy of those
        // configurations, so the project's own repositories populate
        // GRADLE_USER_HOME. The build scripts themselves are not modified.
        def fixDeps = (findProperty('mswb.fixDeps') ?: '').tokenize(';')
            .collect { it.split('=', 2) }.findAll { it[0] == path }.collect { it[1] }
        if (!fixDeps.isEmpty()) {
            tasks.register('mswbResolveFixDeps') {
                doLast {
                    ['compileClasspath', 'testCompileClasspath', 'testRuntimeClasspath'].each { name ->
                        def c = configurations.getByName(name).copyRecursive()
                        fixDeps.each { c.dependencies.add(dependencies.create(it)) }
                        println "mswbResolveFixDeps ${path} ${name}: ${c.resolve().size()} files"
                    }
                }
            }
        }
    }
}
"""

# Test selection: only the test classes test.patch touches, grouped by the
# Gradle project that owns them (path dir/sub -> :dir:sub).
_DERIVE_TARGETS = r"""TEST_FILES="$(sed -nE 's#^diff --git a/.* b/(.*/src/test/java/.*(Test|Tests|TestCase)\.java)$#\1#p' /home/test.patch | sort -u)"
MODULES="$(printf '%s\n' "$TEST_FILES" | sed -nE 's#^(.*)/src/test/java/.*$#\1#p' | sort -u)"
SEL=""
for _m in $MODULES; do
    if [ -f "$_m/build.gradle" ] || [ -f "$_m/build.gradle.kts" ]; then SEL="$SEL $_m"; fi
done
SEL="${SEL# }"
_proj() { printf ':%s' "$(printf '%s' "$1" | sed 's#/#:#g')"; }
"""

# When test.patch references API that only fix.patch adds, javac rejects the
# whole test source set of that project. Files javac names in its errors are
# moved aside (iteratively) until testClasses compiles, then restored before
# the tests run (some tests here read the test sources at runtime), and the
# compile of those projects is excluded from the test invocation. The @Test
# methods test.patch adds/changes in a dropped class are reported as failures.
_RUN_TARGETS = r"""if [ -z "$SEL" ]; then
    echo "NO_TEST_MODULES_AT_THIS_STAGE"
    exit 0
fi

for _m in $SEL; do rm -rf "$_m/build/test-results"; done

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

COMPILE_TASKS=""
for _m in $SEL; do COMPILE_TASKS="$COMPILE_TASKS $(_proj "$_m"):testClasses"; done

STASH=/tmp/mswb_dropped_tests
rm -rf "$STASH"
DROPPED=""
for _attempt in 1 2 3 4 5; do
    if _out="$(@@GRADLE_TIMEOUT@@ @@GRADLE@@ $COMPILE_TASKS 2>&1)"; then
        break
    fi
    _bad="$(printf '%s\n' "$_out" \
        | sed -nE 's#^[[:space:]]*(@@REPO_DIR@@/[^:]*/src/test/java/[^:]*\.java):[0-9]+: error: .*#\1#p' \
        | sort -u)"
    if [ -z "$_bad" ]; then
        printf '%s\n' "$_out" | grep -E 'error|FAILED|What went wrong' | head -40
        break
    fi
    echo "DROPPED_UNCOMPILABLE_TEST_SOURCES (attempt $_attempt):"
    printf '%s\n' "$_bad" | sed 's/^/  /'
    while IFS= read -r _l; do
        [ -n "$_l" ] || continue
        echo "COMPILE_ERRORS_FOR $_l"
        awk -v pat="$_l" 'index($0, pat) && n++ < 20 { print "    " $0 }' <<< "$_out"
        mkdir -p "$STASH$(dirname "$_l")"
        mv "$_l" "$STASH$_l"
    done <<< "$_bad"
    DROPPED="$DROPPED $_bad"
done

# Restore the sources that were only hidden from javac, and do not let Gradle
# recompile those projects' test source sets during the test run.
EXCLUDE=""
for _f in $DROPPED; do
    mv "$STASH$_f" "$_f"
    _rel="${_f#@@REPO_DIR@@/}"
    _mod="${_rel%%/src/test/java/*}"
    case " $EXCLUDE " in
        *" -x $(_proj "$_mod"):compileTestJava "*) ;;
        *) EXCLUDE="$EXCLUDE -x $(_proj "$_mod"):compileTestJava" ;;
    esac
done
rm -rf "$STASH"

TEST_ARGS=""
for _m in $SEL; do
    _filters=""
    for _f in $TEST_FILES; do
        case "$_f" in
            "$_m"/src/test/java/*) ;;
            *) continue ;;
        esac
        [ -f "$_f" ] || continue
        case " $DROPPED " in
            *" @@REPO_DIR@@/$_f "*) continue ;;
        esac
        _fq="$(printf '%s' "${_f#"$_m"/src/test/java/}" | sed -E 's#\.java$##; s#/#.#g')"
        _filters="$_filters --tests $_fq"
    done
    if [ -n "$_filters" ]; then
        TEST_ARGS="$TEST_ARGS $(_proj "$_m"):test$_filters"
    fi
done

GRADLE_RC=0
if [ -n "$TEST_ARGS" ]; then
    @@GRADLE_TIMEOUT@@ @@GRADLE@@ $TEST_ARGS $EXCLUDE || GRADLE_RC=$?
fi

# Two projects here can hold a test class with the same FQCN, so every
# result block is preceded by the Gradle project path that produced it.
for _m in $SEL; do
    echo "MSWB_PROJECT $(_proj "$_m")"
    find "$_m/build/test-results" -name 'TEST-*.xml' -exec cat {} + 2>/dev/null || true
done

for _f in $DROPPED; do
    _cls="$(basename "$_f" .java)"
    _pkg="$(printf '%s' "$_f" | sed -E 's#^.*/src/test/java/##; s#/[^/]*$##; s#/#.#g')"
    _methods="$(_gold_methods "$_cls")"
    if [ -z "$_methods" ]; then
        echo "SYNTHETIC_FAILURES_SKIPPED $_pkg.$_cls (no patch-touched test method found)"
        continue
    fi
    _rel="${_f#@@REPO_DIR@@/}"
    echo "SYNTHESIZED_COMPILE_FAILURES $_pkg.$_cls"
    echo "MSWB_PROJECT $(_proj "${_rel%%/src/test/java/*}")"
    echo "<testsuite name=\"$_pkg.$_cls\">"
    for _m in $_methods; do
        echo "  <testcase name=\"$_m\" classname=\"$_pkg.$_cls\"><failure type=\"CompilationFailure\">test source did not compile at this stage</failure></testcase>"
    done
    echo "</testsuite>"
done

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "GRADLE_EXIT_CODE=$GRADLE_RC"
    exit "$GRADLE_RC"
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

# ---------- Section 2: PROVISION (build time, pinned by this commit's wrapper + build.gradle) ----------
@@DERIVE_TARGETS@@

echo "prepare.sh: projects=[$SEL]"
TASKS=""
for _m in $SEL; do TASKS="$TASKS $(_proj "$_m"):testClasses $(_proj "$_m"):mswbResolveTestDeps"; done
# Fail loudly if module selection came back empty (no silent `gradlew help`).
test -n "$SEL"

# Downloads the pinned Gradle distribution, every plugin and the selected
# projects' test compile + runtime classpaths into ~/.gradle.
@@GRADLE_TIMEOUT_PREPARE@@ @@GRADLE@@ --init-script /home/mswb-init.gradle $TASKS
@@FIX_DEPS@@
# ---------- Section 3: HARD GATE (offline, non-tolerant) ----------
rm -rf */build */*/build
@@GRADLE_TIMEOUT_PREPARE@@ @@GRADLE@@ --offline --init-script /home/mswb-init.gradle $TASKS
rm -rf */build */*/build
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

ENV GRADLE_OPTS="-Xmx512m -Dorg.gradle.daemon=false"

RUN java -version && git config --global --add safe.directory '*'

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


_GRADLE_DEP = re.compile(
    r"""^\s*(?:api|implementation|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly)"""
    r"""\s*\(?\s*["']([\w.\-]+:[\w.\-]+:[\w.\-]+)["']\s*\)?\s*$"""
)


def _fix_added_deps(fix_patch: str) -> list[tuple[str, str]]:
    """(gradle project path, "g:a:v") for every literal external dependency
    that fix.patch adds to a build.gradle (and does not merely re-declare)."""
    out: dict[tuple[str, str], None] = {}
    current = None
    added: list[str] = []
    removed: set[str] = set()

    def flush() -> None:
        if current is None:
            return
        proj = ":" + "/".join(current.split("/")[:-1]).replace("/", ":")
        for coord in added:
            if coord not in removed:
                out[(proj, coord)] = None

    for line in fix_patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            path = line.split(" b/", 1)[-1]
            current = path if path.endswith("/build.gradle") else None
            added, removed = [], set()
            continue
        if current is None or line.startswith(("+++ ", "--- ")):
            continue
        m = _GRADLE_DEP.match(line[1:]) if line[:1] in "+-" else None
        if m:
            (added.append(m.group(1)) if line[0] == "+" else removed.add(m.group(1)))
    flush()
    return list(out)


def _fix_deps_step(fix_patch: str) -> str:
    deps = _fix_added_deps(fix_patch)
    if not deps:
        return ""
    prop = ";".join("%s=%s" % d for d in deps)
    tasks = " ".join(sorted({"%s:mswbResolveFixDeps" % proj for proj, _ in deps}))
    return (
        "\n# Third-party artifacts this PR's fix.patch adds to build scripts\n"
        "# (%s), parsed from its added dependency lines. Only the\n"
        "# artifacts are pre-resolved into GRADLE_USER_HOME, so the graded fix stage\n"
        "# needs no network; the tree stays at the base sha.\n"
        "@@GRADLE_TIMEOUT_PREPARE@@ @@GRADLE@@ --init-script /home/mswb-init.gradle \\\n"
        "    '-Pmswb.fixDeps=%s' %s\n"
    ) % (", ".join(sorted({c for _, c in deps})), prop, tasks)


def _render(body: str, values: dict) -> str:
    for key, value in values.items():
        body = body.replace("@@%s@@" % key, value)
    return body


def _optional_block(block: str) -> str:
    return "\n%s\n" % block if block else ""


class MybatisPlus7222To7222ImageBase(Image):
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
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class MybatisPlus7222To7222ImageDefault(Image):
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
        return MybatisPlus7222To7222ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "GRADLE": _GRADLE,
            "GRADLE_TIMEOUT_PREPARE": _GRADLE_TIMEOUT_PREPARE,
            "GRADLE_TIMEOUT": _GRADLE_TIMEOUT,
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
            File(".", "mswb-init.gradle", _INIT_GRADLE),
            File(
                ".",
                "prepare.sh",
                self._script(_PREPARE.replace("@@FIX_DEPS@@", _fix_deps_step(self.pr.fix_patch))),
            ),
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
# Gradle's JUnit XML names JUnit 5 methods "method()" / "method(String)[1]".
# Drop the parameter signature (keep the invocation index) so a
# compile-failure stub and the real result share one name.
_SIGNATURE = re.compile(r"\([^()]*\)(?=(\[[^\]]*\])?$)")
_PROJECT = re.compile(r"^MSWB_PROJECT (\S+)[ \t]*$", re.MULTILINE)


def _collect(project: str, text: str, passed: set, failed: set, skipped: set) -> None:
    prefix = project + ":" if project else ""
    for m in _TESTCASE.finditer(text):
        attrs = m.group(1)
        nm = _NAME.search(attrs)
        cn = _CLASSNAME.search(attrs)
        if not nm or not cn:
            continue
        test_id = "%s%s#%s" % (
            prefix,
            cn.group(1),
            _SIGNATURE.sub("", nm.group(1).strip()),
        )
        inner = m.group(3) or ""
        if m.group(2) == "/>":
            passed.add(test_id)
        elif "<failure" in inner or "<error" in inner:
            failed.add(test_id)
        elif "<skipped" in inner:
            skipped.add(test_id)
        else:
            passed.add(test_id)


def parse_junit_xml(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    # re.split with one group -> [before, proj1, chunk1, proj2, chunk2, ...]
    parts = _PROJECT.split(clean)
    chunks = [("", parts[0])] + list(zip(parts[1::2], parts[2::2]))
    for project, chunk in chunks:
        _collect(project, chunk, passed, failed, skipped)

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
class MYBATIS_PLUS_7222_TO_7222(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return MybatisPlus7222To7222ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_junit_xml(test_log)
