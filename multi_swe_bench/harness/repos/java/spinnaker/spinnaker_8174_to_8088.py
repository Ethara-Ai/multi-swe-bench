import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TARGETS_SH = r'''cd /home/__REPO_DIR__
GRADLE_ARGS="--no-daemon --console=plain --continue -I /home/init.gradle"

collect_test_files() {
  grep -oE '^diff --git a/[^ ]+' /home/test.patch | sed 's@^diff --git a/@@' | grep -E '/src/test/' | grep -E '\.(java|kt|groovy|scala)$' | sort -u
}

nearest_build_dir() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    if [ -d "$d" ] && ls "$d" 2>/dev/null | grep -E "$2" >/dev/null; then
      echo "$d"
      return 0
    fi
    d=$(dirname "$d")
  done
  return 1
}

gradle_project_path() {
  local d="$1" p
  p=$(grep -hoE "project\(['\"][^'\"]+['\"]\)\.projectDir *= *(new File\()?['\"]\\\$rootDir/$d['\"]" settings.gradle settings.gradle.kts 2>/dev/null | head -n 1 | sed -E "s/^project\(['\"]([^'\"]+)['\"].*/\1/")
  if [ -z "$p" ]; then
    p=":$(echo "$d" | tr '/' ':')"
  fi
  echo "$p"
}

TASKS=""
FILTERS=""
FIRST_DIR=""
for f in $(collect_test_files); do
  cls=$(basename "$f")
  cls=${cls%.*}
  case "$cls" in *IT) continue ;; esac
  d=$(nearest_build_dir "$f" '\.gradle(\.kts)?$') || continue
  [ -n "$FIRST_DIR" ] || FIRST_DIR="$d"
  p=$(gradle_project_path "$d")
  case " $TASKS " in *" $p:test "*) ;; *) TASKS="$TASKS $p:test" ;; esac
  FILTERS="$FILTERS,*.$cls"
done
if [ -z "$TASKS" ]; then
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | sed 's@^diff --git a/@@' | sort -u); do
    d=$(nearest_build_dir "$f" '\.gradle(\.kts)?$') || continue
    [ -n "$FIRST_DIR" ] || FIRST_DIR="$d"
    p=$(gradle_project_path "$d")
    case " $TASKS " in *" $p:test "*) ;; *) TASKS="$TASKS $p:test" ;; esac
  done
fi
[ -n "$TASKS" ] || TASKS=" test"
export TEST_FILTER=$(echo "$FILTERS" | sed 's/^,//')
WARM_TASKS=$(echo "$TASKS" | sed 's/:test\b/:testClasses/g')
[ "$TASKS" != " test" ] || WARM_TASKS=" testClasses"

SERVICE=$(echo "$FIRST_DIR" | cut -d/ -f1)
JDK_MAJOR=$(cat "$SERVICE"/Dockerfile.compile 2>/dev/null | grep -oE 'openjdk-?[0-9]+' | grep -oE '[0-9]+' | head -n 1) || JDK_MAJOR=""
if [ -n "$JDK_MAJOR" ]; then
  for cand in /usr/lib/jvm/java-"$JDK_MAJOR"-openjdk-*; do
    if [ -x "$cand/bin/java" ]; then
      export JAVA_HOME="$cand"
      export PATH="$JAVA_HOME/bin:$PATH"
      break
    fi
  done
fi
echo "service: $SERVICE jdk: $JDK_MAJOR java_home: $JAVA_HOME"

show_targets() {
  echo "gradle tasks: $TASKS"
  echo "test filter: $TEST_FILTER"
}

warm_build() {
  ./gradlew $GRADLE_ARGS $WARM_TASKS
}

run_tests() {
  ./gradlew $GRADLE_ARGS "$@" $TASKS
}

clean_results() {
  find . -path '*/build/test-results' -type d -prune -exec rm -rf {} + 2>/dev/null
}

report_tests() {
  find . -path '*/build/test-results/*/TEST-*.xml' -type f 2>/dev/null | sort | while read -r f; do
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
'''

INIT_GRADLE = """allprojects {
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


def gradle_env(repo: str) -> str:
    return (
        "cat > /home/init.gradle <<'MSWE_INIT_GRADLE'\n"
        + INIT_GRADLE
        + "MSWE_INIT_GRADLE\n"
        + TARGETS_SH.replace("__REPO_DIR__", repo)
    )


class SPINNAKER_8174_TO_8088_ImageBase(Image):
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
        return "eclipse-temurin:25-jdk"

    def image_tag(self) -> str:
        return "base-gradle9-jdk25"

    def workdir(self) -> str:
        return "base-gradle9-jdk25"

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
    GRADLE_OPTS="-Xmx1g"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates unzip openjdk-17-jdk-headless \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class SPINNAKER_8174_TO_8088_ImageDefault(Image):
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
        return SPINNAKER_8174_TO_8088_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def run_script(self, apply: str, echo_patch: bool = False) -> str:
        script = "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
        script += f"cd /home/{self.pr.repo}\n"
        if echo_patch:
            script += _TEST_PATCH_ECHO
        if apply:
            script += f"git apply --whitespace=nowarn {apply}\n"
        script += gradle_env(self.pr.repo)
        script += "show_targets\nclean_results\nrc=0\nrun_tests --offline || rc=$?\nreport_tests\nexit $rc\n"
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
"""
        prepare += gradle_env(repo)
        prepare += "show_targets\nwarm_build || true\nrun_tests || true\n"
        prepare += "java -version\n./gradlew $GRADLE_ARGS --offline $WARM_TASKS\n"
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


@Instance.register("spinnaker", "spinnaker_8174_to_8088")
class SPINNAKER_8174_TO_8088(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SPINNAKER_8174_TO_8088_ImageDefault(self.pr, self._config)

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

    def _added_tests(self, patch_text=""):
        names = set()
        fqn = None
        pending_test = False
        re_path = re.compile(r"src/test/(?:java|kotlin|groovy)/(.+)\.(?:java|kt|groovy)$")
        re_annot = re.compile(r"^\s*@(?:org\.junit\.(?:jupiter\.api\.)?)?Test\b")
        re_fun = re.compile(r"^\s*(?:(?:public|private|protected|internal|override|open|suspend|static|final)\s+)*fun\s+(?:`([^`]+)`|(\w+))\s*\(")
        re_java = re.compile(r"^\s*(?:(?:public|private|protected|static|final|synchronized)\s+)*(?:void|[\w<>\[\],.? ]+)\s+(\w+)\s*\(")
        re_spek = re.compile(r"^\s*it\s*\(\s*\"([^\"\\$]*)\"")
        re_minutest = re.compile(r"^\s*test\s*\(\s*\"([^\"\\$]*)\"")
        re_spock = re.compile(r"^\s*def\s+(?:\"([^\"$]+)\"|'([^']+)')\s*\(")
        for line in (self.pr.test_patch or patch_text or "").splitlines():
            if line.startswith("diff --git "):
                parts = line.split(" b/", 1)
                m = re_path.search(parts[1].strip()) if len(parts) > 1 else None
                fqn = m.group(1).replace("/", ".") if m else None
                pending_test = False
                continue
            if fqn is None or not line.startswith("+") or line.startswith("+++"):
                if not line.startswith("+"):
                    pending_test = False
                continue
            body = line[1:]
            if re_annot.match(body):
                pending_test = True
                continue
            if pending_test:
                m = re_fun.match(body)
                if m:
                    names.add(f"{fqn}#{m.group(1) or m.group(2)}()")
                    pending_test = False
                    continue
                m = re_java.match(body)
                if m:
                    names.add(f"{fqn}#{m.group(1)}()")
                    pending_test = False
                    continue
                if body.strip() and not body.strip().startswith("@"):
                    pending_test = False
            m = re_spek.match(body)
            if m:
                names.add(f"{fqn}#it {m.group(1)}")
                continue
            m = re_minutest.match(body)
            if m:
                names.add(f"{fqn}#{m.group(1)}")
                continue
            m = re_spock.match(body)
            if m:
                names.add(f"{fqn}#{m.group(1) or m.group(2)}")
        return {self._normalize(x) for x in names}

    def _normalize(self, name):
        name = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", "<id>", name)
        name = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<id>", name)
        return name

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
            name = self._normalize(f"{cls}#{method}" if method else cls)
            if not name:
                continue
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        clean_log = re_ansi.sub("", test_log)
        if re.search(
            r"COMPILATION ERROR|Compilation failure|Compilation error|^e: |^> Task :\S*compile\w* FAILED|Execution failed for task '[^']*:compile\w*'",
            clean_log,
            re.MULTILINE,
        ):
            for name in self._added_tests(patch_text):
                if name not in passed_tests:
                    failed_tests.add(name)

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
