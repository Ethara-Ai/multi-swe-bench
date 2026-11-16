import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RE_COMPILE_FAIL = re.compile(r"COMPILATION ERROR|Compilation failure")
_RE_TEST_ANNOT = re.compile(r"@(?:org\.junit(?:\.\w+)*\.)?Test\b(?:\s*\([^)]*\))?")
_RE_LEADING_ANNOT = re.compile(r"^(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)+")
_RE_METHOD_DECL = re.compile(r"\b(\w+)\s*\(")
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


_TEST_PATCH_ECHO = """echo "MSWE_TEST_PATCH_BEGIN"
cat /home/test.patch
echo "MSWE_TEST_PATCH_END"
"""


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

COMMON_FN = r"""
mswe_reactor() {
  REACTOR=" . "
  local q="." cur s n
  while [ -n "$q" ]; do
    cur="${q%% *}"
    if [ "$cur" = "$q" ]; then q=""; else q="${q#* }"; fi
    [ -f "$cur/pom.xml" ] || continue
    for s in $(grep -oE '<module>[^<]+</module>' "$cur/pom.xml" | sed -E 's|</?module>||g'); do
      n=$(realpath -m --relative-to=. "$cur/$s")
      case "$REACTOR" in
        *" $n "*) ;;
        *) REACTOR="$REACTOR$n "; q="${q:+$q }$n" ;;
      esac
    done
  done
}

mswe_targets() {
  mswe_reactor
  MODS=""
  TESTS=""
  FQTESTS=""
  while read -r p; do
    case "$p" in
      *src/test/java/*.java) ;;
      *) continue ;;
    esac
    b=$(basename "$p" .java)
    case "$b" in
      *Test|*Tests|*TestCase) ;;
      *) continue ;;
    esac
    case "$p" in
      src/test/java/*) m=. ;;
      *) m="${p%%/src/test/java/*}" ;;
    esac
    [ -f "$m/pom.xml" ] || [ -f "$m/build.gradle" ] || [ -f "$m/build.gradle.kts" ] || continue
    if [ -f pom.xml ]; then
      case "$REACTOR" in *" $m "*) ;; *) continue ;; esac
    fi
    fq="${p#*src/test/java/}"
    fq="${fq%.java}"
    fq="${fq//\//.}"
    case ",$MODS," in *",$m,"*) ;; *) MODS="${MODS:+$MODS,}$m" ;; esac
    case ",$TESTS," in *",$b,"*) ;; *) TESTS="${TESTS:+$TESTS,}$b" ;; esac
    FQTESTS="$FQTESTS $fq"
  done < <(grep -E '^diff --git ' /home/test.patch | sed -E 's|^diff --git a/.* b/(.*)$|\1|' | sort -u)
  if [ -z "$MODS" ]; then
    while read -r p; do
      d=$(dirname "$p")
      while [ "$d" != "." ] && [ ! -f "$d/pom.xml" ] && [ ! -f "$d/build.gradle" ] && [ ! -f "$d/build.gradle.kts" ]; do
        d=$(dirname "$d")
      done
      [ "$d" != "." ] || continue
      if [ -f pom.xml ]; then
        case "$REACTOR" in *" $d "*) ;; *) continue ;; esac
      fi
      case ",$MODS," in *",$d,"*) ;; *) MODS="${MODS:+$MODS,}$d" ;; esac
    done < <(grep -E '^diff --git ' /home/test.patch | sed -E 's|^diff --git a/.* b/(.*)$|\1|' | sort -u)
  fi
  echo "mswe targets: modules=[$MODS] tests=[$TESTS]"
}

mswe_mvn() {
  if [ -x ./mvnw ] && ! grep -qs mvnd .mvn/wrapper/maven-wrapper.properties; then
    MVN=./mvnw
  else
    MVN=mvn
  fi
}
"""

JDK_FN = r"""
mswe_java_candidates() {
  f="$1"
  d=0
  while [ -f "$f" ] && [ "$d" -lt 8 ]; do
    grep -oE '<(maven\.compiler\.(release|source|target)|[A-Za-z.]*java\.version|release)>[0-9][0-9.]*<' "$f" | grep -oE '[0-9][0-9.]*'
    grep -oE 'JavaLanguageVersion\.of\( *[0-9]+' "$f" | grep -oE '[0-9]+$'
    case "$f" in
      *.gradle|*.kts) break ;;
    esac
    rp=$(sed -n '/<parent>/,/<\/parent>/p' "$f" | grep -oE '<relativePath>[^<]*' | sed 's/<relativePath>//')
    [ -n "$rp" ] || rp=../pom.xml
    nf="$(dirname "$f")/$rp"
    [ -d "$nf" ] && nf="$nf/pom.xml"
    f="$nf"
    d=$((d + 1))
  done
}

mswe_detect_jdk() {
  c=$(
    [ -f .java-version ] && head -n1 .java-version
    [ -f .sdkmanrc ] && grep -oE '^java=[0-9]+' .sdkmanrc | grep -oE '[0-9]+'
    for f in pom.xml build.gradle build.gradle.kts; do
      [ -f "$f" ] && mswe_java_candidates "$f"
    done
    for m in ${MODS//,/ }; do
      for f in "$m/pom.xml" "$m/build.gradle" "$m/build.gradle.kts"; do
        [ -f "$f" ] && mswe_java_candidates "$f"
      done
    done
  )
  max=8
  for v in $c; do
    v=${v#1.}
    v=${v%%.*}
    case "$v" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "$v" -gt "$max" ] && max=$v
  done
  for l in 8 11 17 21 25; do
    if [ "$l" -ge "$max" ]; then
      echo "$l"
      return
    fi
  done
  echo 25
}

mswe_install_jdk() {
  v=$(mswe_detect_jdk)
  echo "mswe jdk: $v"
  apt-get update
  apt-get install -y --no-install-recommends "temurin-$v-jdk"
  ln -sfn "/usr/lib/jvm/temurin-$v-jdk-$(dpkg --print-architecture)" /usr/lib/jvm/mswe-jdk
  java -version
}
"""

REPORT_FN = r"""
mswe_report() {
  find . \( -path '*/target/surefire-reports/TEST-*.xml' -o -path '*/build/test-results/*/TEST-*.xml' \) -type f 2>/dev/null | sort | while read -r f; do
    awk -v OFS='\t' '
      function attr(s, a,   r) {
        if (match(s, " " a "=\"[^\"]*\"")) {
          r = substr(s, RSTART + length(a) + 3, RLENGTH - length(a) - 4)
          return r
        }
        return ""
      }
      function emit() {
        if (open) { print "MSWE_TEST", st, cls, nm; open = 0 }
      }
      /<testcase[ >]/ {
        emit()
        t = substr($0, index($0, "<testcase"))
        e = index(t, ">")
        head = substr(t, 1, e)
        nm = attr(head, "name")
        cls = attr(head, "classname")
        st = "PASSED"
        open = 1
        if (substr(head, e - 1, 1) == "/") { emit(); next }
        rest = substr(t, e + 1)
        if (rest ~ /<(failure|error)[ >\/]/) st = "FAILED"
        else if (rest ~ /<skipped[ >\/]/) st = "SKIPPED"
        if (rest ~ /<\/testcase>/) emit()
        next
      }
      open && /^[ \t]*<(failure|error)[ >\/]/ { st = "FAILED" }
      open && /^[ \t]*<skipped[ >\/]/ { if (st != "FAILED") st = "SKIPPED" }
      open && /<\/testcase>/ { emit() }
      END { emit() }
    ' "$f"
  done
}
"""

CLEAN_REPORTS = r"""
find . \( -path '*/target/surefire-reports' -o -path '*/build/test-results' \) -type d -prune -exec rm -rf {} + 2>/dev/null
"""

BASE_RUN = ""

PREPARE_EXTRA = ""

PRE_TEST = ""

BUILD_CMD = r"""
$MVN -B -ntp -nsu -Djacoco.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dcheckstyle.skip=true -Drat.skip=true -Dspotbugs.skip=true -Dlicense.skip=true -Denforcer.skip=true -DskipDepCheck=true -Ddocker.build.skip=true -Dbuild.ui.skip=true -Dmaven.javadoc.skip=true -DskipUT=false install -DskipTests ${MODS:+-pl "$MODS" -am}
"""

TEST_CMD = r"""
$MVN -B -ntp -nsu -Djacoco.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dcheckstyle.skip=true -Drat.skip=true -Dspotbugs.skip=true -Dlicense.skip=true -Denforcer.skip=true -DskipDepCheck=true -Ddocker.build.skip=true -Dbuild.ui.skip=true -Dmaven.javadoc.skip=true -DskipUT=false test ${MODS:+-pl "$MODS" -am} ${TESTS:+-Dtest="$TESTS"} -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false
"""


GATE_CMD = r"""
$MVN -B -ntp -nsu -o -Djacoco.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dcheckstyle.skip=true -Drat.skip=true -Dspotbugs.skip=true -Dlicense.skip=true -Denforcer.skip=true -DskipDepCheck=true -Ddocker.build.skip=true -Dbuild.ui.skip=true -Dmaven.javadoc.skip=true test-compile ${MODS:+-pl "$MODS" -am}
"""

PRUNE_BLOCK = r"""RUN set -eux; \
    cd /home/__REPO__; \
    test "$(git rev-parse HEAD)" = "__SHA__"; \
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
    test "$(git rev-parse HEAD)" = "__SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        cd /home/__REPO__ && git submodule foreach --recursive ' \
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
"""



class Dolphinscheduler18715To17822ImageBase(Image):
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
        return "ubuntu:24.04"

    def image_tag(self) -> str:
        return "base-autojdk-mvnw"

    def workdir(self) -> str:
        return "base-autojdk-mvnw"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f"""RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        else:
            code = f"COPY {repo} /home/{repo}"

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CI=true \\
    MAVEN_OPTS="-Xmx3g -XX:+ExitOnOutOfMemoryError" \\
    JAVA_HOME=/usr/lib/jvm/mswe-jdk \\
    PATH=/usr/lib/jvm/mswe-jdk/bin:$PATH

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends git curl wget ca-certificates unzip gnupg jq \\
    && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/adoptium.list \\
    && apt-get update && apt-get install -y --no-install-recommends temurin-8-jdk \\
    && ln -sfn "/usr/lib/jvm/temurin-8-jdk-$(dpkg --print-architecture)" /usr/lib/jvm/mswe-jdk \\
    && java -version \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | tar -xz -C /opt \\
    && ln -sf /opt/apache-maven-3.9.11/bin/mvn /usr/local/bin/mvn
{BASE_RUN}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Dolphinscheduler18715To17822ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return Dolphinscheduler18715To17822ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_script(self, apply: str, echo_patch: bool = False) -> str:
        script = "#!/bin/bash\nset -eo pipefail\nexport CI=true\n"
        script += f"cd /home/{self.pr.repo}\n"
        if echo_patch:
            script += _TEST_PATCH_ECHO
        script += COMMON_FN + REPORT_FN
        script += "mswe_targets\nmswe_mvn\n"
        if apply:
            script += f"git apply --whitespace=nowarn {apply}\n"
        script += PRE_TEST
        script += CLEAN_REPORTS
        script += "rc=0\n"
        script += TEST_CMD.strip() + " || rc=$?\n"
        script += "mswe_report\nexit $rc\n"
        return script

    def files(self) -> list[File]:
        prepare = """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch origin {sha}
git checkout --detach {sha}
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo, sha=self.pr.base.sha)
        prepare += COMMON_FN + JDK_FN
        prepare += "mswe_targets\nmswe_install_jdk || true\nmswe_mvn\n"
        if PREPARE_EXTRA.strip():
            prepare += "{\n" + PREPARE_EXTRA.strip() + "\n} || true\n"
        prepare += BUILD_CMD.strip() + " || true\n"
        if PRE_TEST.strip():
            prepare += "{\n" + PRE_TEST.strip() + "\n} || true\n"
        prepare += TEST_CMD.strip() + " || true\n"
        prepare += CLEAN_REPORTS.strip() + " || true\n"
        prepare += "java -version\n" + GATE_CMD.strip() + "\n"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""",
            ),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self.run_script("")),
            File(".", "test-run.sh", self.run_script("/home/test.patch", echo_patch=True)),
            File(".", "fix-run.sh", self.run_script("/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prune = PRUNE_BLOCK.replace("__REPO__", self.pr.repo).replace(
            "__SHA__", self.pr.base.sha
        )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{prune}
{self.clear_env}

"""


@Instance.register("apache", "dolphinscheduler_18715_to_17822")
class DOLPHINSCHEDULER_18715_TO_17822(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Dolphinscheduler18715To17822ImageDefault(self.pr, self._config)

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
        re_ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_log, patch_text = _split_patch_block(re_ansi.sub("", test_log))
        re_line = re.compile(r"^MSWE_TEST\t(PASSED|FAILED|SKIPPED)\t([^\t]*)\t(.*)$")

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for raw in re_ansi.sub("", test_log).splitlines():
            m = re_line.match(raw.rstrip("\r"))
            if not m:
                continue
            status, cls, method = m.group(1), m.group(2).strip(), m.group(3).strip()
            name = f"{cls}#{method}" if method else cls
            if not name:
                continue
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        if _RE_COMPILE_FAIL.search(test_log):
            failed_tests |= _added_test_names(self.pr.test_patch or patch_text, "#") - passed_tests

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
