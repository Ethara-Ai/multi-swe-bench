import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RE_PATCH_BLOCK = re.compile(
    r"^[^\n]*MSWE_TEST_PATCH_BEGIN[^\n]*\n(.*?)(?:MSWE_TEST_PATCH_END[^\n]*(?:\n|$)|\Z)",
    re.S | re.M,
)
_RE_COMPILE_FAIL = re.compile(r"COMPILATION ERROR|Compilation failure")


def _patch_test_classes(patch: str) -> set:
    found = set()
    for m in re.finditer(r"^diff --git a/\S+ b/(\S+/src/test/java/(\S+)\.java)$", patch or "", re.M):
        name = m.group(2).rsplit("/", 1)[-1]
        if name.startswith("Test") or name.endswith("Test") or name.endswith("Tests"):
            found.add(m.group(2).replace("/", "."))
    return found


TARGETS_SH = r'''cd /home/__REPO_DIR__
SKIP_FLAGS="-Dair.check.skip-all=true -Dcheckstyle.skip -Dspotless.check.skip -Denforcer.skip -Dmaven.javadoc.skip -Dlicense.skip -Dmaven.source.skip -Dmodernizer.skip -Dskip.bun -Dskip.installbun"
INSTALL_FLAGS=""

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

MODULES=""
TESTS=""
for f in $(collect_test_files); do
  cls=$(basename "$f")
  cls=${cls%.*}
  case "$cls" in *IT) continue ;; esac
  d=$(nearest_build_dir "$f" '^pom\.xml$') || continue
  case ",$MODULES," in *",$d,"*) ;; *) MODULES="$MODULES,$d" ;; esac
  TESTS="$TESTS,$cls"
done
if [ -z "$MODULES" ]; then
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | sed 's@^diff --git a/@@' | sort -u); do
    d=$(nearest_build_dir "$f" '^pom\.xml$') || continue
    case ",$MODULES," in *",$d,"*) ;; *) MODULES="$MODULES,$d" ;; esac
  done
fi
MODULES=$(echo "$MODULES" | sed 's/^,//')
TESTS=$(echo "$TESTS" | sed 's/^,//')

show_targets() {
  echo "test modules: $MODULES"
  echo "test classes: $TESTS"
}

warm_build() {
  ./mvnw -B install -DskipTests ${MODULES:+-pl "$MODULES" -am} $SKIP_FLAGS $INSTALL_FLAGS
}

run_tests() {
  ./mvnw -B -fae "$@" test ${MODULES:+-pl "$MODULES"} $SKIP_FLAGS ${TESTS:+-Dtest="$TESTS"} -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false
}
'''


class TRINO_29295_TO_29295_ImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        code = "" if self.config.need_clone else f"COPY {repo} /home/{repo}"

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
    MAVEN_OPTS="-Xmx4g"

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

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates unzip \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class TRINO_29295_TO_29295_ImageDefault(Image):
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
        return TRINO_29295_TO_29295_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run_script(self, apply: str) -> str:
        script = f"#!/bin/bash\nset -eo pipefail\nexport CI=true\n\ncd /home/{self.pr.repo}\n"
        if apply == "/home/test.patch":
            script += 'echo "MSWE_TEST_PATCH_BEGIN"\ncat /home/test.patch\necho "MSWE_TEST_PATCH_END"\n'
        if apply:
            script += f"git apply --whitespace=nowarn {apply}\n"
        return script + "source /home/targets.sh\nshow_targets\nrun_tests -o\n"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),

            File(".", "targets.sh", TARGETS_SH.replace("__REPO_DIR__", self.pr.repo)),
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
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git init -q
git remote get-url origin >/dev/null 2>&1 || git remote add origin https://github.com/{pr.org}/{pr.repo}.git
git fetch --depth 1 --no-tags origin {pr.base.sha}
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
bash /home/check_git_changes.sh
source /home/targets.sh
show_targets
warm_build
run_tests || true
./mvnw -B -q test-compile ${{MODULES:+-pl "$MODULES"}} $SKIP_FLAGS
echo DEPS_OK
""".format(pr=self.pr),
            ),
            File(".", "run.sh", self._run_script("")),
            File(".", "test-run.sh", self._run_script("/home/test.patch")),
            File(".", "fix-run.sh", self._run_script("/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {name}:{tag}

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

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("trinodb", "trino_29295_to_29295")
class TRINO_29295_TO_29295(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TRINO_29295_TO_29295_ImageDefault(self.pr, self._config)

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
        re_running = re.compile(r"\[INFO\] Running ([A-Za-z_$][\w$]*(?:\.[\w$]+)+)\s*$", re.M)
        re_perclass = re.compile(
            r"\[(?:INFO|ERROR|WARNING)\] Tests run: (\d+), Failures: (\d+), "
            r"Errors: (\d+), Skipped: (\d+),\s+Time elapsed:.* -+ in (\S+)"
        )
        re_fork_crash = re.compile(r"Crashed tests:\s*\n\s*\[ERROR\]\s+([\w.$]+)")

        text = re_ansi.sub("", test_log)
        m = _RE_PATCH_BLOCK.search(text)
        block = m.group(1) if m else ""
        if m:
            text = text[: m.start()] + text[m.end():]

        passed_tests = set()
        skipped_tests = set()
        failed_tests = set()
        summarized = set()

        for line in text.split("\n"):
            m = re_perclass.search(line)
            if not m:
                continue
            tests_run = int(m.group(1))
            failures = int(m.group(2))
            errors = int(m.group(3))
            skipped = int(m.group(4))
            cls = m.group(5)
            summarized.add(cls)
            if failures > 0 or errors > 0:
                failed_tests.add(cls)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(cls)
            else:
                passed_tests.add(cls)

        for cls in {m.group(1) for m in re_running.finditer(text)} - summarized:
            failed_tests.add(cls)
        for m in re_fork_crash.finditer(text):
            failed_tests.add(m.group(1))
        if _RE_COMPILE_FAIL.search(text):
            failed_tests |= _patch_test_classes(self.pr.test_patch or block)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
