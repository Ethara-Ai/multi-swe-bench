import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.26.11-bookworm"
_REPO_DIR = "/home/beats"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RUN = re.compile(r"^=== RUN\s+(\S+)")
_RESULT = re.compile(r"--- (PASS|FAIL|SKIP): (\S+) \(")
_PKG_DONE = re.compile(r"^(ok|FAIL|\?)\s+(\S+)")

_SENTINEL = 'test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'

_RUN_ENV = """export CI=true
export TZ=UTC
export CGO_ENABLED=1
export GOFLAGS=-buildvcs=false
export GOTOOLCHAIN=local
export GOPROXY=https://proxy.golang.org,direct
export GOSUMDB=sum.golang.org
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""

_SELECT_PKGS = """PKGS=""
for DIR in {dirs}; do
  if ls "$DIR"/*.go > /dev/null 2>&1; then
    PKGS="$PKGS ./$DIR"
  fi
done
"""

_TEST_COMMAND = """{select_pkgs}if [ -z "$PKGS" ]; then
  echo "no test packages present at this stage"
  exit 0
fi
go test -v -count=1 -timeout 30m $PKGS
"""


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _test_dirs(pr: PullRequest) -> list[str]:
    dirs = []
    for path in _patch_files(pr.test_patch) + _patch_files(pr.fix_patch):
        if not path.endswith("_test.go"):
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        if directory not in dirs:
            dirs.append(directory)
    return sorted(dirs)


def _select_pkgs(pr: PullRequest) -> str:
    return _SELECT_PKGS.format(dirs=" ".join(_test_dirs(pr)) or ".")


def _test_command(pr: PullRequest) -> str:
    return _TEST_COMMAND.format(select_pkgs=_select_pkgs(pr))


class Beats54557To48638ImageBase(Image):
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
        return _GO_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {_GO_IMAGE}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git curl build-essential make libpcap-dev libsystemd-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

# {_SENTINEL}

CMD ["/bin/bash"]
"""


class Beats54557To48638ImageDefault(Image):
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
        return Beats54557To48638ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_command = _test_command(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd {repo_dir}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

{run_env}
{select_pkgs}
go version || true
go list -e -deps -test $PKGS > /dev/null || true
git diff --name-only -z -- go.mod go.sum | xargs -0 -r git checkout -- || true

test "$(git rev-parse HEAD)" = "{base_sha}"
go version
GOPROXY=off GOFLAGS="-mod=readonly -buildvcs=false" go list -e -deps -test -f '{{{{if .Error}}}}{{{{.ImportPath}}}}: {{{{.Error}}}}{{{{end}}}}' $PKGS > /tmp/go-deps-check.log
if grep -E "module lookup disabled|cannot find module|missing go.sum entry|no required module|not in std" /tmp/go-deps-check.log; then
  echo "prepare.sh: dependency resolution incomplete"
  exit 1
fi
echo "DEPS_OK $(go version | awk '{{print $3}}')"

bash /home/check_git_changes.sh
""".format(
                    repo_dir=_REPO_DIR,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    select_pkgs=_select_pkgs(self.pr),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
{test_command}""".format(
                    repo_dir=_REPO_DIR, run_env=_RUN_ENV, test_command=test_command
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch

{test_command}""".format(
                    repo_dir=_REPO_DIR, run_env=_RUN_ENV, test_command=test_command
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd {repo_dir}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}""".format(
                    repo_dir=_REPO_DIR, run_env=_RUN_ENV, test_command=test_command
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}"""


@Instance.register("elastic", "beats")
class BEATS_54557_TO_48638(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Beats54557To48638ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        started: list[str] = []
        pending: dict[str, str] = {}

        def record(status: str, name: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            elif status == "SKIP":
                if name in failed_tests or name in passed_tests:
                    return
                skipped_tests.add(name)

        for raw in _ANSI.sub("", log).splitlines():
            line = raw.strip()

            match = _RUN.match(line)
            if match:
                if match.group(1) not in started:
                    started.append(match.group(1))
                continue

            match = _RESULT.search(line)
            if match:
                status, name = match.group(1), match.group(2)
                if pending.get(name) != "FAIL":
                    pending[name] = status
                continue

            match = _PKG_DONE.match(line)
            if match:
                outcome, package = match.group(1), match.group(2)
                if outcome == "FAIL":
                    for name in started:
                        pending.setdefault(name, "FAIL")
                for name, status in pending.items():
                    record(status, f"{package}::{name}")
                started.clear()
                pending.clear()

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
