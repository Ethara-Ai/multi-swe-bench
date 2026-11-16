import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_LINE = re.compile(r"^MSWE_TEST\t(PASSED|FAILED|SKIPPED)\t([^\t]*)\t(.*)$")
_RE_COMPILE_FAIL = re.compile(r"COMPILATION ERROR|Compilation failure")
_RE_PATCH_BLOCK = re.compile(
    r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)", re.S | re.M
)
_RE_TEST_ANNOT = re.compile(r"@(?:org\.junit(?:\.\w+)*\.)?(Test|RepeatedTest)\b(?:\s*\(\s*(?:value\s*=\s*)?(\d*)[^)]*\))?")
_RE_LEADING_ANNOT = re.compile(r"^(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)+")
_RE_METHOD_DECL = re.compile(r"\b(\w+)\s*\(\s*(\)?)")
_RE_CLASS_DECL = re.compile(r"(?<![.\w])(?:class|interface|enum|record)\s+(\w+)")
_RE_LITERAL = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_RE_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")
_JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized"}


def _added_test_names(test_patch: str) -> set:
    found, path, stack, depth, tracked = {}, None, [], 0, False
    pending = {"+": None, "-": None}
    for line in (test_patch or "").splitlines():
        if line.startswith("diff --git "):
            path = None
            continue
        if line.startswith(("--- ", "+++ ")):
            p = re.sub(r"^[ab]/", "", line[4:].strip())
            if line.startswith("--- ") or p != "/dev/null":
                path = p if "/src/test/java/" in p and p.endswith(".java") else None
            continue
        h = _RE_HUNK.match(line)
        if path is None or h or line.startswith("\\"):
            if h:
                tracked = h.group(1) in ("0", "1")
                stack, depth, pending = [], 0, {"+": None, "-": None}
            continue
        kind = line[:1]
        kinds = [kind] if kind in pending else ["+", "-"]
        body = _RE_LITERAL.sub('""', line[1:]).strip()
        m = _RE_TEST_ANNOT.match(body)
        if m:
            for k in kinds:
                pending[k] = int(m.group(2)) if m.group(1) == "RepeatedTest" and m.group(2) else 0
            body = body[m.end():].strip()
        if body.startswith(("//", "*", "/*")):
            continue
        body = _RE_LEADING_ANNOT.sub("", body).strip()
        if not body:
            continue
        fqn = path.split("/src/test/java/", 1)[1][: -len(".java")].replace("/", ".")
        if tracked and len(stack) > 1:
            fqn += "$" + "$".join(n for n, _ in stack[1:])
        sets = found.setdefault(path, {"+": set(), "-": set()})
        is_method = False
        for k in kinds:
            mm = next((x for x in _RE_METHOD_DECL.finditer(body) if x.group(1) not in _JAVA_KEYWORDS), None)
            if pending[k] is not None and kind in pending and mm:
                is_method = True
                if pending[k] and mm.group(2):
                    sets[k].update(f"{fqn}#{mm.group(1)}()[{i}]" for i in range(1, pending[k] + 1))
                else:
                    sets[k].add(f"{fqn}#{mm.group(1)}")
            pending[k] = None
        if kind == "-" or not tracked:
            continue
        c = _RE_CLASS_DECL.search(body)
        if c and not is_method:
            stack.append((c.group(1), depth))
        depth += body.count("{") - body.count("}")
        while stack and depth <= stack[-1][1]:
            stack.pop()
    return set().union(*(v["+"] - v["-"] for v in found.values()))


COMMON_FN = r"""
mswe_add() {
  local p="$1" b m fq
  [[ "$p" == *src/test/java/*.java ]] || return 0
  b=$(basename "$p" .java)
  [[ "$b" =~ (Test|Tests|TestCase)$ ]] || return 0
  [ "$2" != offline ] || mswe_offline_ok "$p" || return 0
  m="${p%%/src/test/java/*}"
  [ -f "$m/pom.xml" ] && grep -qF "<module>${m##*/}</module>" "$(dirname "$m")/pom.xml" || return 0
  fq=$(sed -E 's|.*src/test/java/||; s|\.java$||; s|/|.|g' <<< "$p")
  case " $FQTESTS " in *" $fq "*) return 0 ;; esac
  if [ -f "$p" ]; then FOUND=1; fi
  case ",$MODS," in *",$m,"*) ;; *) MODS="${MODS:+$MODS,}$m" ;; esac
  case ",$TESTS," in *",$b,"*) ;; *) TESTS="${TESTS:+$TESTS,}$b" ;; esac
  FQTESTS="$FQTESTS $fq"
  return 0
}

mswe_offline_ok() {
  case "$1" in *IT.java) return 1 ;; esac
  ! grep -qE 'org\.testcontainers|@Testcontainers|@SpringBootTest|@EnabledIfEnvironmentVariable|@EnabledIfSystemProperty' "$1"
}

mswe_scan() {
  find "$@" -type f \( -name '*Test.java' -o -name '*Tests.java' -o -name '*TestCase.java' \) -not -path '*/target/*' -not -path '*/.git/*' 2>/dev/null | sed 's|^\./||' | sort
}

mswe_add_all() {
  while read -r p; do
    [ -z "$p" ] || mswe_add "$p" "$1"
  done
}

mswe_targets() {
  MODS="" TESTS="" FQTESTS="" FOUND=0
  PATCHED=$(grep -E '^diff --git ' /home/test.patch | sed -E 's|^diff --git a/.* b/(.*)$|\1|' | sort -u)
  mswe_add_all <<< "$PATCHED"
  while read -r d; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    mswe_add_all offline < <(mswe_scan "$d" -maxdepth 1)
  done < <(printf '%s\n' "$PATCHED" | grep -E 'src/test/java/.*\.java$' | xargs -r -n1 dirname | sort -u)
  if [ "$FOUND" = 0 ] && [ -n "$MODS" ]; then
    for m in ${MODS//,/ }; do
      [ -d "$m/src/test/java" ] && mswe_add_all offline < <(mswe_scan "$m/src/test/java")
    done
  fi
  [ "$FOUND" != 0 ] || [ -z "$TESTS" ] || mswe_add_all offline < <(mswe_scan . -path '*/src/test/java/*')
  echo "mswe targets: modules=[$MODS] tests=[$TESTS]"
}

mswe_mvn() {
  MVN=mvn
  if [ -x ./mvnw ] && ! grep -qs mvnd .mvn/wrapper/maven-wrapper.properties; then MVN=./mvnw; fi
}
"""

JDK_FN = r"""
mswe_install_jdk() {
  v=$(head -n1 .java-version 2>/dev/null | cut -d. -f1)
  v=${v:-21}
  echo "mswe jdk: $v"
  apt-get update
  apt-get install -y --no-install-recommends "temurin-$v-jdk"
  ln -sfn "/usr/lib/jvm/temurin-$v-jdk-$(dpkg --print-architecture)" /usr/lib/jvm/mswe-jdk
  java -version
}
"""

REPORT_FN = r"""
mswe_report() {
  find . -path '*/target/surefire-reports/TEST-*.xml' -type f 2>/dev/null | sort | while read -r f; do
    perl -0777 -ne '
      while (/<testcase\b([^>]*?)(?:\/>|>(.*?)<\/testcase>)/sg) {
        my ($h, $b) = ($1, $2 // "");
        my ($n) = $h =~ / name="([^"]*)"/;
        my ($c) = $h =~ / classname="([^"]*)"/;
        my $s = $b =~ /<(failure|error)[ >\/]/ ? "FAILED" : $b =~ /<skipped[ >\/]/ ? "SKIPPED" : "PASSED";
        print "MSWE_TEST\t$s\t", $c // "", "\t", $n // "", "\n";
      }
    ' "$f"
  done
}
"""

CLEAN_REPORTS = "find . -path '*/target/surefire-reports' -type d -prune -exec rm -rf {} + 2>/dev/null"
MVN_FLAGS = "$MVN -B -ntp -nsu -Djacoco.skip=true -Denforcer.skip=true -Dspotless.check.skip=true -Dlicense.skip=true -Dmaven.javadoc.skip=true -DskipAf4RecipeTests=true"
MODS_ARG = '${MODS:+-pl "$MODS" -am}'
BUILD_CMD = f"{MVN_FLAGS} install -DskipTests {MODS_ARG}"
TEST_CMD = f'{MVN_FLAGS} test {MODS_ARG} ${{TESTS:+-Dtest="$TESTS"}} -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false'
GATE_CMD = f"{MVN_FLAGS} -q test-compile {MODS_ARG}"

CHECK_GIT_CHANGES = """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "check_git_changes: Uncommitted changes"; exit 1; }
echo "check_git_changes: No uncommitted changes"
"""


class _Axonframework4997To4997Image(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def image_tag(self) -> str:
        return self.workdir()


class Axonframework4997To4997ImageBase(_Axonframework4997To4997Image):
    def dependency(self) -> str | Image:
        return "ubuntu:24.04"

    def workdir(self) -> str:
        return "base-autojdk-solo"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"
        crt = "/etc/ssl/certs/ca-certificates.crt"
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
ARG http_proxy="" https_proxy="" HTTP_PROXY="" HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="{crt}"

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC CI=true \\
    http_proxy=${{http_proxy}} https_proxy=${{https_proxy}} HTTP_PROXY=${{HTTP_PROXY}} HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    MAVEN_OPTS="-Xmx3g -XX:+ExitOnOutOfMemoryError" \\
    JAVA_HOME=/usr/lib/jvm/mswe-jdk

ENV PATH=/usr/lib/jvm/mswe-jdk/bin:$PATH

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    for t in /etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem /etc/ssl/ca-bundle.pem /etc/pki/tls/cacert.pem \\
        /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/ssl/certs/ca-bundle.crt; do ln -sf {crt} "$t"; done

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git curl wget ca-certificates unzip gnupg jq \\
    && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb $(. /etc/os-release && echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/adoptium.list \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.tar.gz | tar -xz -C /opt \\
    && ln -sf /opt/apache-maven-3.9.11/bin/mvn /usr/local/bin/mvn

RUN git config --global --add safe.directory '*'

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Axonframework4997To4997ImageDefault(_Axonframework4997To4997Image):
    def dependency(self) -> Image | None:
        return Axonframework4997To4997ImageBase(self.pr, self._config)

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_script(self, apply: str, echo_patch: bool = False) -> str:
        script = f"#!/bin/bash\nset -eo pipefail\nexport CI=true\ncd /home/{self.pr.repo}\n"
        if echo_patch:
            script += 'echo "MSWE_TEST_PATCH_BEGIN"\ncat /home/test.patch\necho "MSWE_TEST_PATCH_END"\n'
        script += COMMON_FN + REPORT_FN + "mswe_targets\nmswe_mvn\n"
        if apply:
            script += f"git apply --whitespace=nowarn {apply}\n"
        script += f"{CLEAN_REPORTS}\nrc=0\n{TEST_CMD} || rc=$?\nmswe_report\nexit $rc\n"
        return script

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        prepare = f"""#!/bin/bash
set -e
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {sha}^{{commit}} 2>/dev/null || git fetch origin {sha}
git checkout --detach {sha}
bash /home/check_git_changes.sh
{COMMON_FN}{JDK_FN}mswe_targets
mswe_install_jdk || true
mswe_mvn
{BUILD_CMD} || true
{TEST_CMD} || true
{CLEAN_REPORTS} || true
test "$(git rev-parse HEAD)" = "{sha}"
java -version
{GATE_CMD}
echo DEPS_OK
"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self.run_script("")),
            File(".", "test-run.sh", self.run_script("/home/test.patch", echo_patch=True)),
            File(".", "fix-run.sh", self.run_script("/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
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

{self.clear_env}
"""


@Instance.register("AxonIQ", "axonframework_4997_to_4997")
class AXONFRAMEWORK_4997_TO_4997(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Axonframework4997To4997ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = _RE_ANSI.sub("", test_log)
        block = _RE_PATCH_BLOCK.search(test_log)
        if block:
            test_log = test_log[: block.start()] + test_log[block.end():]
        found = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for raw in test_log.splitlines():
            m = _RE_LINE.match(raw.rstrip("\r"))
            if m:
                cls, method = m.group(2).strip(), m.group(3).strip()
                name = f"{cls}#{method}" if method else cls
                if name:
                    found[m.group(1)].add(name)
        passed_tests, failed_tests, skipped_tests = found["PASSED"], found["FAILED"], found["SKIPPED"]
        if _RE_COMPILE_FAIL.search(test_log):
            failed_tests |= _added_test_names(self.pr.test_patch or (block.group(1) if block else "")) - passed_tests
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
