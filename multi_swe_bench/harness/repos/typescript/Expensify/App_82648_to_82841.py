import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NODE_IMAGE = "node:20.19.5-bookworm"

_BASE_TAG = "base-82648_to_82841"

_TEST_ROOTS = ("tests/ui/", "tests/unit/", "tests/actions/", "tests/navigation/")

_ENV = """\
export CI=true
export TZ=utc
export NODE_OPTIONS="--experimental-vm-modules --max-old-space-size=8192"
export npm_config_update_notifier=false
export npm_config_fund=false
export npm_config_audit=false
"""

_JEST_REPORT = """\
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const root = process.cwd() + "/";
const statusMap = { passed: "PASSED", failed: "FAILED" };
for (const suite of data.testResults || []) {
  const file = String(suite.name || suite.testFilePath || "").split(root).join("");
  const results = suite.assertionResults || [];
  if (results.length === 0 && suite.status === "failed") {
    console.log("JEST_SUITE_FAILED " + file);
  }
  for (const result of results) {
    const title = [file, ...(result.ancestorTitles || []), result.title].join(" > ").replace(/\\s+/g, " ").trim();
    const status = statusMap[result.status] || "SKIPPED";
    console.log("JEST_RESULT " + status + " " + title);
  }
}
console.log("JEST_SUMMARY suites=" + (data.numTotalTestSuites || 0) + " tests=" + (data.numTotalTests || 0) + " passed=" + (data.numPassedTests || 0) + " failed=" + (data.numFailedTests || 0));
"""

_CHECK_GIT_CHANGES = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """\
#!/bin/bash
set -euo pipefail

__ENV__
export npm_config_fetch_retries=8
export npm_config_fetch_retry_mintimeout=20000
export npm_config_fetch_retry_maxtimeout=180000
export npm_config_fetch_timeout=900000
cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

node --version
npm --version
test -f package-lock.json

for attempt in 1 2 3; do
  if npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund; then
    break
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "npm ci failed after $attempt attempts"
    exit 1
  fi
  rm -rf node_modules
  sleep 20
done

bash scripts/applyPatches.sh

test -f /home/jest_report.js

node_modules/.bin/jest --ci --listTests > /home/list-tests.log 2>&1 || { tail -n 80 /home/list-tests.log; exit 1; }
tail -n 5 /home/list-tests.log
grep -qE "[.]tsx?$" /home/list-tests.log

node -e "require.resolve('jest'); require.resolve('jest-expo'); require.resolve('babel-jest'); require('./package.json'); console.log('DEPS_OK', process.version)"
"""

_STAGE = """\
#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
__APPLY__
rm -f /home/jest-results.json
STATUS=0
node_modules/.bin/jest --ci --silent --runInBand --forceExit --json --outputFile=/home/jest-results.json __SCOPE__ || STATUS=$?
if [ -f /home/jest-results.json ]; then
  node /home/jest_report.js /home/jest-results.json
else
  echo "=== NO JEST RESULTS FILE WRITTEN"
fi
exit $STATUS
"""

_PRUNE = """\
RUN set -eux; \\
    cd /home/__REPO__; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.windowMemory 256m; \\
    git config --local pack.threads 2; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        cd /home/__REPO__ && git submodule foreach --recursive ' \\
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
"""


def _test_scope(pr: PullRequest) -> str:
    paths: list[str] = []
    for match in re.finditer(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch or "", re.M):
        path = match.group(1)
        if path.startswith(_TEST_ROOTS) and re.search(r"[.]tsx?$", path) and path not in paths:
            paths.append(path)
    return " ".join(shlex.quote(re.escape(path) + "$") for path in paths)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__ENV__", _ENV)
        .replace("__SCOPE__", _test_scope(pr))
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__REPO__", pr.repo)
    )


class ExpensifyApp82648To82841ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = DockerfileEnhancer._infrastructure_block(self, _NODE_IMAGE)
        repo = self.pr.repo
        return (
            f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}\n\n"
            f"FROM {_NODE_IMAGE}\n\n"
            f"{infra}\n"
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}\n\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates jq \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "RUN git config --global --add safe.directory '*' && \\\n"
            "    git config --global --add url.\"https://github.com/\".insteadOf \"ssh://git@github.com/\" && \\\n"
            "    git config --global --add url.\"https://github.com/\".insteadOf \"git@github.com:\"\n\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
            f"    cd /home/{repo} && git rev-parse HEAD >/dev/null\n\n"
            'CMD ["/bin/bash"]\n'
        )


class ExpensifyApp82648To82841ImageDefault(Image):
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
        return ExpensifyApp82648To82841ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "jest_report.js", _JEST_REPORT),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_STAGE, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n\n"
            f"{_render(_PRUNE, self.pr)}"
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_RESULT_RE = re.compile(r"^JEST_RESULT (PASSED|FAILED|SKIPPED) (.+)$")


@Instance.register("Expensify", "App_82648_to_82841")
class APP_82648_TO_82841(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ExpensifyApp82648To82841ImageDefault(self.pr, self._config)

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
        occurrences: dict[str, int] = {}

        for raw_line in _ANSI_RE.sub("", test_log).split("\n"):
            match = _RESULT_RE.match(raw_line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            occurrences[name] = occurrences.get(name, 0) + 1
            if occurrences[name] > 1:
                name = f"{name} [{occurrences[name]}]"
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

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
