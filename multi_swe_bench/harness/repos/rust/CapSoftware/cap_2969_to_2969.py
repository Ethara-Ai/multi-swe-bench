import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_BUN_VERSION = "1.4.0"
_TEST_RE = r"^apps/web/__tests__/.+\.test\.tsx?$"

_ENV = """export CI=true
export TZ=UTC
export NO_COLOR=1
export FORCE_COLOR=0
export NODE_DISABLE_COLORS=1
export DO_NOT_TRACK=1
export NEXT_TELEMETRY_DISABLED=1
export TURBO_TELEMETRY_DISABLED=1
export NODE_OPTIONS=--max-old-space-size=6144
export npm_config_update_notifier=false"""

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

_REPORT = r"""const fs = require("fs");
const [file, rel] = process.argv.slice(2);
let data = null;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (e) {
  process.exit(0);
}
const clean = (s) => String(s).replace(/\s+/g, " ").trim();
const seen = new Map();
for (const result of data.testResults || []) {
  for (const a of result.assertionResults || []) {
    const parts = [...(a.ancestorTitles || []), a.title].map(clean).filter(Boolean);
    let name = `${rel}::${parts.join(" > ")}`;
    const n = (seen.get(name) || 0) + 1;
    seen.set(name, n);
    if (n > 1) name = `${name} [${n}]`;
    const status = a.status === "passed" ? "PASSED" : a.status === "failed" ? "FAILED" : "SKIPPED";
    console.log(`MSWE_CASE ${status} ${name}`);
  }
}
"""


def _run_tests(repo: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
source /home/mswe-select.sh
mapfile -t FILES < <(mswe_pick '{_TEST_RE}' existing)
if [ "${{#FILES[@]}}" -eq 0 ]; then
  echo "run-tests: no test.patch test files present in this tree"
  exit 0
fi
VITEST=/home/{repo}/apps/web/node_modules/.bin/vitest
[ -x "$VITEST" ] || VITEST=/home/{repo}/node_modules/.bin/vitest
status=0
for f in "${{FILES[@]}}"; do
  echo "MSWE_CTX $f"
  rm -f /tmp/mswe-vitest.json
  if (cd apps/web && "$VITEST" run --reporter=default --reporter=json --outputFile.json=/tmp/mswe-vitest.json "${{f#apps/web/}}"); then
    echo "MSWE_FILE PASSED $f"
  else
    echo "MSWE_FILE FAILED $f"
    status=1
  fi
  node /home/mswe-report.cjs /tmp/mswe-vitest.json "$f" || status=1
done
exit $status
"""


def _prepare(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -e
{_ENV}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

retry() {{
  local attempt
  for attempt in 1 2 3; do
    if "$@"; then
      return 0
    fi
    sleep 30
  done
  return 1
}}
retry bun install --frozen-lockfile --ignore-scripts || true

test "$(git rev-parse HEAD)" = "{sha}"
bun install --frozen-lockfile --ignore-scripts
test -x apps/web/node_modules/.bin/vitest || test -x node_modules/.bin/vitest
echo DEPS_OK
"""


class CapRangeImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"range-base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"range-base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["unzip"]

    def extra_setup(self) -> str:
        return (
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
            "    CI=true\n"
            f"RUN npm install -g bun@{_BUN_VERSION} \\\n"
            "    && git config --global --add safe.directory '*' \\\n"
            "    && node --version && bun --version"
        )


class CapRangeImageDefault(Image):
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
        return CapRangeImageBase(self.pr, self._config)

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
            File(".", "mswe-report.cjs", _REPORT),
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
_RESULT = re.compile(r"^MSWE_(?:CASE|FILE) (PASSED|FAILED|SKIPPED) (.+)$")


@Instance.register("CapSoftware", "cap_2969_to_2969")
class CapRange(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CapRangeImageDefault(self.pr, self._config)

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
            "FAILED": failed_tests,
            "SKIPPED": skipped_tests,
        }
        for raw in _ANSI.sub("", test_log).splitlines():
            found = _RESULT.match(raw.strip())
            if found:
                buckets[found.group(1)].add(found.group(2).strip())

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
