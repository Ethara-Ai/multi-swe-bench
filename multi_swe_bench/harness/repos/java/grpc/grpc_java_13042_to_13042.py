import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RE_COMPILE_FAIL = re.compile(r"compileTest\w* FAILED|COMPILATION ERROR|Compilation failure|\.java:\d+:? (?:error:|\[error\])")
_RE_PATCH_BLOCK = re.compile(r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)", re.S | re.M)
_RE_TEST_ANNOT = re.compile(r"@(?:org\.junit(?:\.\w+)*\.)?Test\b(?:\s*\([^)]*\))?")
_RE_LEADING_ANNOT = re.compile(r"^(?:@[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)+")
_RE_METHOD_DECL = re.compile(r"\b(\w+)\s*\(")
_JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "synchronized"}
_TEST_PATCH_ECHO = 'echo "MSWE_TEST_PATCH_BEGIN"\ncat /home/test.patch\necho "MSWE_TEST_PATCH_END"\n'


def _added_test_names(patch):
    found, path, pending = {"+": set(), "-": set()}, None, set()
    for line in (patch or "").splitlines():
        if line.startswith(("diff --git ", "+++ ")):
            p = "/" + line[4:].strip().removeprefix("b/") if line.startswith("+++ ") else ""
            ok = "/src/test/java/" in p and p.endswith(".java")
            path = p.split("/src/test/java/", 1)[1][:-5].replace("/", ".") if ok else None
            pending = set()
            continue
        if path is None or line.startswith(("--- ", "\\", "@@")):
            pending = set() if line.startswith("@@") else pending
            continue
        kind = line[:1]
        kinds = {kind} if kind in found else {"+", "-"}
        body = line[1:].strip()
        m = _RE_TEST_ANNOT.match(body)
        if m:
            pending |= kinds
            body = body[m.end():].strip()
        if not body or body.startswith(("//", "*", "/*")):
            continue
        body = _RE_LEADING_ANNOT.sub("", body).strip()
        if not body:
            continue
        mm = next((x for x in _RE_METHOD_DECL.finditer(body) if x.group(1) not in _JAVA_KEYWORDS), None)
        for k in kinds & pending:
            if kind in found and mm:
                found[k].add(f"{path}#{mm.group(1)}")
        pending -= kinds
    return found["+"] - found["-"]


TARGETS_SH = r'''cd /home/__REPO_DIR__
GRADLE_ARGS="--no-daemon --console=plain --continue -PskipCodegen=true -PskipAndroid=true -PerrorProne=false -I /home/init.gradle"

collect_test_files() { grep -oE '^diff --git a/[^ ]+' /home/test.patch | sed 's@^diff --git a/@@' | grep -E '/src/test/' | grep -E '\.(java|kt|groovy|scala)$' | sort -u; }

nearest_build_dir() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    [ -d "$d" ] && ls "$d" 2>/dev/null | grep -E "$2" >/dev/null && { echo "$d"; return 0; }
    d=$(dirname "$d")
  done
  return 1
}

gradle_project_path() {
  local d="$1" p
  p=$(grep -hoE "project\(['\"][^'\"]+['\"]\)\.projectDir *= *(new File\()?['\"]\\\$rootDir/$d['\"]" settings.gradle settings.gradle.kts 2>/dev/null | head -n 1 | sed -E "s/^project\(['\"]([^'\"]+)['\"].*/\1/")
  [ -n "$p" ] || p=":$(echo "$d" | tr '/' ':')"
  echo "$p"
}

TASKS=""
FILTERS=""
for f in $(collect_test_files); do
  cls=$(basename "${f%.*}")
  case "$cls" in *IT) continue ;; esac
  d=$(nearest_build_dir "$f" '\.gradle(\.kts)?$') || continue
  p=$(gradle_project_path "$d")
  case " $TASKS " in *" $p:test "*) ;; *) TASKS="$TASKS $p:test" ;; esac
  FILTERS="$FILTERS,*.$cls"
done
if [ -z "$TASKS" ]; then
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | sed 's@^diff --git a/@@' | sort -u); do
    d=$(nearest_build_dir "$f" '\.gradle(\.kts)?$') || continue
      p=$(gradle_project_path "$d")
    case " $TASKS " in *" $p:test "*) ;; *) TASKS="$TASKS $p:test" ;; esac
  done
fi
[ -n "$TASKS" ] || TASKS=" test"
export TEST_FILTER=$(echo "$FILTERS" | sed 's/^,//')
WARM_TASKS=$(echo "$TASKS" | sed 's/:test\b/:testClasses/g')
[ "$TASKS" != " test" ] || WARM_TASKS=" testClasses"

show_targets() { echo "gradle tasks: $TASKS"; echo "test filter: $TEST_FILTER"; }
warm_build() { ./gradlew $GRADLE_ARGS $WARM_TASKS; }
run_tests() { ./gradlew $GRADLE_ARGS "$@" $TASKS; }
clean_results() { find . -path '*/build/test-results' -type d -prune -exec rm -rf {} + 2>/dev/null; }

report_tests() {
  find . -path '*/build/test-results/*/TEST-*.xml' -type f 2>/dev/null | sort | while read -r f; do
    awk -v OFS='\t' '
      function attr(s, a) {
        if (match(s, " " a "=\"[^\"]*\"")) return substr(s, RSTART + length(a) + 3, RLENGTH - length(a) - 4)
        return ""
      }
      function emit() { if (open) { print "MSWE_TEST", st, cls, nm; open = 0 } }
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
'''

RUN_TAIL = """source /home/targets.sh
show_targets
clean_results
rc=0
run_tests --offline || rc=$?
report_tests
exit $rc
"""

PRUNE = r"""RUN set -eux; \
    cd /home/{repo}; \
    git checkout --detach "{sha}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "{sha}"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \
        cd /home/{repo} && git submodule foreach --recursive ' \
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


class GRPC_JAVA_13042_TO_13042_ImageBase(Image):
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
        return "eclipse-temurin:17-jdk"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    GRADLE_OPTS="-Xmx2g"

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates unzip \\
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /root/.gradle && echo "org.gradle.jvmargs=-Xmx3g" >> /root/.gradle/gradle.properties

RUN git config --global --add safe.directory '*'

{code}
{self.clear_env}
CMD ["/bin/bash"]
"""


class GRPC_JAVA_13042_TO_13042_ImageDefault(GRPC_JAVA_13042_TO_13042_ImageBase):
    def dependency(self) -> Image | None:
        return GRPC_JAVA_13042_TO_13042_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_script(self, apply: str) -> str:
        script = f"#!/bin/bash\nset -eo pipefail\nexport CI=true\n\ncd /home/{self.pr.repo}\n"
        if apply:
            script += _TEST_PATCH_ECHO if apply == "/home/test.patch" else ""
            script += f"git apply --whitespace=nowarn {apply}\n"
        return script + RUN_TAIL

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "init.gradle", """allprojects {
    tasks.withType(Test).configureEach {
        outputs.upToDateWhen { false }
        testLogging {
            events "passed", "skipped", "failed"
            exceptionFormat = "short"
        }
        def testFilter = System.getenv("TEST_FILTER")
        if (testFilter) {
            filter {
                testFilter.split(",").each { includeTestsMatching(it) }
                failOnNoMatchingTests = false
            }
        }
    }
}
"""),
            File(".", "targets.sh", TARGETS_SH.replace("__REPO_DIR__", self.pr.repo)),
            File(".", "check_git_changes.sh", """#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo "check_git_changes: Uncommitted changes"; exit 1; }
echo "check_git_changes: No uncommitted changes"
"""),
            File(".", "prepare.sh", """#!/bin/bash
set -e
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch origin {pr.base.sha}
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh
source /home/targets.sh
show_targets
warm_build || true
run_tests || true
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
java -version
warm_build
echo DEPS_OK
""".format(pr=self.pr)),
            File(".", "run.sh", self.run_script("")),
            File(".", "test-run.sh", self.run_script("/home/test.patch")),
            File(".", "fix-run.sh", self.run_script("/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        prune = PRUNE.replace("{repo}", self.pr.repo).replace("{sha}", self.pr.base.sha)

        return f"""FROM {self.dependency().image_full_name()}

{self.global_env}
{copy_commands}
RUN bash /home/prepare.sh

{prune}
{self.clear_env}
"""


@Instance.register("grpc", "grpc-java")
@Instance.register("grpc", "grpc_java_13042_to_13042")
class GRPC_JAVA_13042_TO_13042(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return GRPC_JAVA_13042_TO_13042_ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        block = _RE_PATCH_BLOCK.search(test_log)
        patch_text = block.group(1) if block else ""
        test_log = test_log[: block.start()] + test_log[block.end():] if block else test_log
        re_line = re.compile(r"^MSWE_TEST\t(PASSED|FAILED|SKIPPED)\t([^\t]*)\t(.*)$")
        tests = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for raw in test_log.splitlines():
            m = re_line.match(raw.rstrip("\r"))
            if m:
                cls, method = m.group(2).strip(), m.group(3).strip()
                name = f"{cls}#{method}" if method else cls
                if name:
                    tests[m.group(1)].add(name)
        failed_tests = tests["FAILED"]
        if _RE_COMPILE_FAIL.search(test_log):
            failed_tests |= _added_test_names(self.pr.test_patch or patch_text) - tests["PASSED"]
        passed_tests = tests["PASSED"] - failed_tests
        skipped_tests = tests["SKIPPED"] - failed_tests - passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
