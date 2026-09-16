
import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_6452_to_71 import (
    LobeHubImageBase,
)

PM_PROBE = (
    "node -e \"try {{ const pm = require('./package.json').packageManager;"
    " console.log(pm && pm.startsWith('pnpm@') ? 'pnpm' : 'npm'); }}"
    " catch (e) {{ console.log('npm'); }}\""
)

PNPM_VERSION_PROBE = (
    "node -e \"try {{ const pm = require('./package.json').packageManager;"
    " console.log(pm.split('@')[1]); }} catch (e) {{ console.log('latest'); }}\""
)

VITEST_ARGS = "vitest run --reporter=verbose"

SHELL_ENV = """export CI=true
export NO_COLOR=1
export NODE_OPTIONS="--max-old-space-size=4096"
export NEXT_TELEMETRY_DISABLED=1"""


class LobeHubImageDefault2024(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return LobeHubImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        env = SHELL_ENV

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

{env}

cd /home/{repo}

bash /home/check_git_changes.sh

export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=180000
export npm_config_fetch_timeout=600000

retry_install() {{
  attempt=1
  while [ "$attempt" -le 3 ]; do
    echo "prepare: install attempt $attempt of 3"
    if "$@"; then
      return 0
    fi
    if [ "$attempt" -eq 3 ]; then
      echo "prepare: install failed after 3 attempts" >&2
      return 1
    fi
    echo "prepare: install attempt $attempt failed, retrying in 30s" >&2
    sleep 30
    attempt=$((attempt + 1))
  done
}}

PKG_MANAGER=$({PM_PROBE})
echo "prepare: package manager $PKG_MANAGER (from package.json)"

if [ "$PKG_MANAGER" = "pnpm" ]; then
  PNPM_VERSION=$({PNPM_VERSION_PROBE})
  echo "prepare: installing pnpm@$PNPM_VERSION"
  retry_install npm install -g "pnpm@$PNPM_VERSION"
  retry_install pnpm install --no-frozen-lockfile
else
  retry_install npm install --legacy-peer-deps
fi

npx vitest --version

TEST_FILES=$(find src -name '*.test.ts' -o -name '*.test.tsx' | wc -l)
echo "prepare: $TEST_FILES test files under src/"
if [ "$TEST_FILES" -lt 1 ]; then
  echo "prepare: no test files found under src/, tree is not what this config expects" >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
npx {VITEST_ARGS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx {VITEST_ARGS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx {VITEST_ARGS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, str):
            raise ValueError("ImageDefault dependency must be an Image")

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean_test_name(name: str) -> str:
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


_PASS_RE = re.compile(r"^[✓✔]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$")
_FAIL_RE = re.compile(r"^[×✕✗]\s+(.+?)(?:\s+\(?\d+(?:\.\d+)?\s*m?s\)?)?$")
_SKIP_RE = re.compile(r"^[↓○]\s+(.+?)(?:\s+\[skipped\])?$")
_FILE_FAIL_RE = re.compile(r"^FAIL\s+(.+?)$")


def parse_vitest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = _PASS_RE.match(line)
        if m:
            passed_tests.add(_clean_test_name(m.group(1)))
            continue

        m = _FAIL_RE.match(line) or _FILE_FAIL_RE.match(line)
        if m:
            failed_tests.add(_clean_test_name(m.group(1)))
            continue

        m = _SKIP_RE.match(line)
        if m:
            skipped_tests.add(_clean_test_name(m.group(1)))

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


@Instance.register("lobehub", "lobehub_3206_to_2231")
class LOBEHUB_3206_TO_2231(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LobeHubImageDefault2024(self.pr, self._config)

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
        return parse_vitest_log(test_log)
