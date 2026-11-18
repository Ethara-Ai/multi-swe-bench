import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.97.0-bookworm"
_TEST_RE = r"^cli/tests/[^/]+\.rs$"

_ENV = """export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export RUST_BACKTRACE=1
unset AGENT_BROWSER_EXECUTABLE_PATH AGENT_BROWSER_SOCKET_DIR XDG_RUNTIME_DIR
PROFILE=""
grep -qE '^\\[profile\\.ci\\]' cli/Cargo.toml && PROFILE="--profile ci\""""

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

_SELECT = """#!/bin/bash
mapfile -t MSWE_CHANGED < <(sed -n -E 's#^diff --git a/[^ ]+ b/([^ ]+)$#\\1#p' /home/test.patch | sort -u)
mswe_pick() {
  local re="$1" mode="$2" f
  for f in "${MSWE_CHANGED[@]}"; do
    if [[ "$f" =~ $re ]]; then
      if [ "$mode" != "existing" ] || [ -f "$f" ]; then
        echo "$f"
      fi
    fi
  done
}
"""


def _run_tests(repo: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
{_ENV}
export CARGO_NET_OFFLINE=true

source /home/mswe-select.sh
mapfile -t FILES < <(mswe_pick '{_TEST_RE}' existing)
if [ "${{#FILES[@]}}" -eq 0 ]; then
  echo "run-tests: no test.patch integration targets present in this tree"
  exit 0
fi
status=0
for f in "${{FILES[@]}}"; do
  echo "MSWE_CTX $f"
  if cargo test $PROFILE --manifest-path cli/Cargo.toml --test "$(basename "$f" .rs)"; then
    echo "MSWE_FILE PASSED $f"
  else
    echo "MSWE_FILE FAILED $f"
    status=1
  fi
done
exit $status
"""


def _prepare(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -e
cd /home/{repo}
{_ENV}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

retry() {{
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@"; then
      return 0
    fi
    sleep 20
  done
  return 1
}}
retry cargo fetch --locked --manifest-path cli/Cargo.toml || true
cargo metadata --locked --offline --format-version 1 --manifest-path cli/Cargo.toml > /dev/null
CARGO_NET_OFFLINE=true cargo test $PROFILE --manifest-path cli/Cargo.toml --tests --no-run || true

test "$(git rev-parse HEAD)" = "{sha}"
echo DEPS_OK
"""


class AgentBrowserRangeImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"range-base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"range-base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["pkg-config", "libssl-dev"]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    CI=true\n"
            "RUN git config --global --add safe.directory '*' \\\n"
            "    && rustc --version && cargo --version"
        )


class AgentBrowserRangeImageDefault(Image):
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
        return AgentBrowserRangeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswe-select.sh", _SELECT),
            File(".", "prepare.sh", _prepare(repo, self.pr.base.sha)),
            File(".", "run-tests.sh", _run_tests(repo)),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || exit 1
bash /home/run-tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || exit 1
bash /home/run-tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {base.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_CTX = re.compile(r"^MSWE_CTX (\S+)$")
_FILE = re.compile(r"^MSWE_FILE (PASSED|FAILED) (\S+)$")
_CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")


@Instance.register("vercel-labs", "agent_browser_2128_to_2128")
class AgentBrowserRange(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AgentBrowserRangeImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {
            "PASSED": passed_tests,
            "ok": passed_tests,
            "FAILED": failed_tests,
            "ignored": skipped_tests,
        }
        ctx = ""
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            found = _CTX.match(line)
            if found:
                ctx = found.group(1)
                continue
            found = _FILE.match(line)
            if found:
                buckets[found.group(1)].add(found.group(2))
                continue
            found = _CARGO.match(line)
            if found:
                buckets[found.group(2)].add(f"{ctx}::{found.group(1)}")

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
