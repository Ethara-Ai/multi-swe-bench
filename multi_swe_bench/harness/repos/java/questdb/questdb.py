import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RE_COMPILE_FAIL = re.compile(r"COMPILATION ERROR|Compilation failure")
_RE_TEST_ANNOT = re.compile(r"@(?:org\.junit(?:\.\w+)*\.)?Test\b(?:\s*\([^)]*\))?")
_RE_LEADING_ANNOT = re.compile(r"^(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)+")
_RE_METHOD_DECL = re.compile(r"\b(\w+)\s*\(")
_RE_PARAM_SUFFIX = re.compile(r"^(.*?::[^\[]+)\[.*\]$")
_RE_JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized"}


_RE_PATCH_BLOCK = re.compile(
    r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)",
    re.S | re.M,
)


def _split_patch_block(log: str):
    m = _RE_PATCH_BLOCK.search(log or "")
    if not m:
        return log, ""
    return log[: m.start()] + log[m.end():], m.group(1).replace("\r\n", "\n")


def _added_test_names(test_patch: str, sep: str) -> set:
    found = {"+": set(), "-": set()}
    pending = {"+": False, "-": False}
    path = None
    for line in (test_patch or "").splitlines():
        if line.startswith("diff --git "):
            path = None
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            path = p if "/src/test/java/" in p and p.endswith(".java") else None
            pending = {"+": False, "-": False}
            continue
        if path is None or line.startswith("--- ") or line.startswith("\\"):
            continue
        if line.startswith("@@"):
            pending = {"+": False, "-": False}
            continue
        kind = line[:1]
        kinds = [kind] if kind in found else ["+", "-"]
        body = line[1:].strip()
        m = _RE_TEST_ANNOT.match(body)
        if m:
            for k in kinds:
                pending[k] = True
            body = body[m.end():].strip()
        if not body or body.startswith("//") or body.startswith("*") or body.startswith("/*"):
            continue
        body = _RE_LEADING_ANNOT.sub("", body).strip()
        if not body:
            continue
        fqn = path.split("/src/test/java/", 1)[1][: -len(".java")].replace("/", ".")
        for k in kinds:
            if pending[k] and kind in found:
                for mm in _RE_METHOD_DECL.finditer(body):
                    if mm.group(1) not in _RE_JAVA_KEYWORDS:
                        found[k].add(f"{fqn}{sep}{mm.group(1)}")
                        break
            pending[k] = False
    return found["+"] - found["-"]


_TARGETS = r"""TEST_FILES="$(grep -E '^diff --git a/.*/src/test/java/.*\.java b/' /home/test.patch | sed -E 's#^diff --git a/(.*) b/.*$#\1#' | sort -u)" || TEST_FILES=""
CLASSES=""
MODULES=""
for f in $TEST_FILES; do
  c="$(basename "$f" .java)"
  case "$c" in
    Abstract*) continue ;;
    *Test|*Tests|*IT) ;;
    *) continue ;;
  esac
  d="$(dirname "$f")"
  while [ "$d" != "." ] && [ "$d" != "/" ] && [ ! -f "$d/pom.xml" ]; do d="$(dirname "$d")"; done
  if [ "$d" = "." ] || [ "$d" = "/" ] || [ ! -f "$d/pom.xml" ]; then continue; fi
  CLASSES="$CLASSES,$c"
  case ",$MODULES," in *",$d,"*) ;; *) MODULES="$MODULES,$d" ;; esac
done
if [ -z "$CLASSES" ]; then
  for f in $(grep -E '^diff --git a/' /home/test.patch | sed -E 's#^diff --git a/(.*) b/.*$#\1#' | sort -u); do
    d="$(dirname "$f")"
    while [ "$d" != "." ] && [ "$d" != "/" ] && [ ! -f "$d/pom.xml" ]; do d="$(dirname "$d")"; done
    if [ "$d" = "." ] || [ "$d" = "/" ] || [ ! -f "$d/pom.xml" ]; then continue; fi
    case "$f" in
      */src/test/java/*) c="$(dirname "${f#*/src/test/java/}")/**/*Test" ;;
      *) c="" ;;
    esac
    if [ -n "$c" ]; then
      case ",$CLASSES," in *",$c,"*) ;; *) CLASSES="$CLASSES,$c" ;; esac
    fi
    case ",$MODULES," in *",$d,"*) ;; *) MODULES="$MODULES,$d" ;; esac
  done
fi
CLASSES="${CLASSES#,}"
MODULES="${MODULES#,}"
"""

_EXCLUDES = """EXCLUDES=""
for p in $(grep -E '^Binary files .* differ$' /home/fix.patch /home/test.patch | sed -E 's#^[^:]*:Binary files (.*) and (.*) differ$#\\1 \\2#'); do
  case "$p" in
    /dev/null) ;;
    a/*|b/*) EXCLUDES="$EXCLUDES --exclude=${p#?/}" ;;
  esac
done
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PLATFORM = """case "$(uname -m)" in
  aarch64|arm64) QDB_PLAT=linux-aarch64 ;;
  *) QDB_PLAT=linux-x86-64 ;;
esac
"""

_PREPARE = """#!/bin/bash
set -e

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e @@SHA@@^{commit} 2>/dev/null || git fetch origin @@SHA@@
git checkout --detach @@SHA@@
git submodule update --init --recursive java-questdb-client core/src/main/c/share/jemalloc core/src/main/c/share/simdjson core/src/main/c/share/zlib core/src/main/c/share/fsst || true
bash /home/check_git_changes.sh

@@PLATFORM@@
cd /home/@@REPO@@/java-questdb-client/core
cmake -DCMAKE_BUILD_TYPE=Release -B /home/qdb-client-native -S . || true
cmake --build /home/qdb-client-native --parallel "$(nproc)" || true
mkdir -p src/main/resources/io/questdb/client/bin/$QDB_PLAT
cp target/classes/io/questdb/client/bin-local/libquestdb.so src/main/resources/io/questdb/client/bin/$QDB_PLAT/libquestdb.so || true
cd /home/@@REPO@@/java-questdb-client
mvn -B -q install -DskipTests -Dmaven.javadoc.skip=true -pl core -am || true

cd /home/@@REPO@@/core
cmake -DCMAKE_BUILD_TYPE=Release -B /home/qdb-native -S . || true
cmake --build /home/qdb-native --parallel "$(nproc)" || true
rm -rf /home/qdb-native-out
cp -a target/classes/io/questdb/bin-local /home/qdb-native-out || true
rm -rf target questdb

cd /home/@@REPO@@
mvn -B clean test -pl core -Dmaven.javadoc.skip=true -Dtest=DirectLongHashSetTest -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false || true

java -version
test -f /home/qdb-native/CMakeCache.txt
test -d /home/qdb-native-out
test -f core/src/main/resources/io/questdb/bin/$QDB_PLAT/libquestdb.so
mvn -B -o -q test -pl core -Dmaven.javadoc.skip=true -Dtest=DirectLongHashSetTest -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false && mvn -B -o -q clean -pl core && echo DEPS_OK
"""

_REPORT = r"""find . -path '*/target/surefire-reports/TEST-*.xml' 2>/dev/null | sort | while read -r f; do
  awk '
    function attr(s, k,   r) {
      if (match(s, " " k "=\"[^\"]*\"")) {
        r = substr(s, RSTART + length(k) + 3, RLENGTH - length(k) - 4)
        return r
      }
      return ""
    }
    function emit() {
      if (cur != "") print "QDB_TESTCASE " st " " cur
      cur = ""
    }
    /^[ \t]*<testcase / {
      emit()
      cur = attr($0, "classname") "::" attr($0, "name")
      st = "PASSED"
      if ($0 ~ /\/>[ \t]*$/) emit()
      next
    }
    cur != "" && /^[ \t]*<(failure|error)[ \t\/>]/ { st = "FAILED" }
    cur != "" && /^[ \t]*<skipped[ \t\/>]/ { if (st == "PASSED") st = "SKIPPED" }
    cur != "" && /<\/testcase>/ { emit() }
    END { emit() }
  ' "$f"
done
"""

_RUN_TESTS = """cd /home/@@REPO@@
@@TARGETS@@
rc=0
if [ -n "$CLASSES" ] && [ -n "$MODULES" ]; then
  mvn -B -o clean test -pl "$MODULES" -Dmaven.javadoc.skip=true -Dstyle.color=never -Dmaven.test.redirectTestOutputToFile=true -Dfuzz.s0=1 -Dfuzz.s1=1 -Dtest="$CLASSES" -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false || rc=$?
else
  mvn -B -o clean test -pl "${MODULES:-core}" -Dmaven.javadoc.skip=true -Dstyle.color=never -Dmaven.test.redirectTestOutputToFile=true -Dfuzz.s0=1 -Dfuzz.s1=1 || rc=$?
fi
@@REPORT@@
exit $rc"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
@@RUN_TESTS@@
"""

_TEST_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
echo "MSWE_TEST_PATCH_BEGIN"
cat /home/test.patch
echo "MSWE_TEST_PATCH_END"
git apply --whitespace=nowarn /home/test.patch
@@RUN_TESTS@@
"""

_FIX_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
@@EXCLUDES@@
git apply --whitespace=nowarn $EXCLUDES /home/test.patch /home/fix.patch
if grep -q '^diff --git a/core/src/main/c/' /home/fix.patch; then
  @@PLATFORM@@
  cd /home/@@REPO@@/core
  rm -rf target questdb
  mkdir -p target/classes/io/questdb
  cp -a /home/qdb-native-out target/classes/io/questdb/bin-local
  cmake --build /home/qdb-native --parallel "$(nproc)" && cp target/classes/io/questdb/bin-local/libquestdb.so src/main/resources/io/questdb/bin/$QDB_PLAT/libquestdb.so
  rm -rf target questdb
fi
@@RUN_TESTS@@
"""


def _fill(body: str, pr: PullRequest) -> str:
    return (
        body.replace("@@RUN_TESTS@@", _RUN_TESTS)
        .replace("@@REPORT@@", _REPORT)
        .replace("@@PLATFORM@@", _PLATFORM)
        .replace("@@TARGETS@@", _TARGETS)
        .replace("@@EXCLUDES@@", _EXCLUDES)
        .replace("@@REPO@@", pr.repo)
        .replace("@@ORG@@", pr.org)
        .replace("@@SHA@@", pr.base.sha)
    )


def _pr_dockerfile(image: Image) -> str:
    base = image.dependency()
    repo = image.pr.repo
    sha = image.pr.base.sha
    copy_commands = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    return f"""FROM {base.image_full_name()}

{image.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{image.clear_env}
"""


class QuestdbImageBase(Image):
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
        return "eclipse-temurin:25-jdk-noble"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV MAVEN_OPTS="-Xmx2g" \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl cmake g++ make nasm \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | tar -xz -C /opt && \\
    ln -sf /opt/apache-maven-3.9.11/bin/mvn /usr/local/bin/mvn && \\
    mvn -v && java -version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class QuestdbImageDefault(Image):
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
        return QuestdbImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _fill(_PREPARE, self.pr)),
            File(".", "run.sh", _fill(_RUN, self.pr)),
            File(".", "test-run.sh", _fill(_TEST_RUN, self.pr)),
            File(".", "fix-run.sh", _fill(_FIX_RUN, self.pr)),
        ]

    def dockerfile(self) -> str:
        return _pr_dockerfile(self)


@Instance.register("questdb", "questdb")
@Instance.register("questdb", "questdb_7681_to_7655")
class Questdb(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return QuestdbImageDefault(self.pr, self._config)

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
        re_case = re.compile(r"^QDB_TESTCASE (PASSED|FAILED|SKIPPED) (\S.*?)\s*$")
        test_log, patch_text = _split_patch_block(re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log))

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            m = re_case.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            name = _RE_PARAM_SUFFIX.sub(r"\1", name)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if _RE_COMPILE_FAIL.search(test_log):
            failed_tests |= _added_test_names(self.pr.test_patch or patch_text, "::") - passed_tests

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
