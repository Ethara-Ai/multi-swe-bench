import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NODE_IMAGE = "node:16-bookworm"

_BASE_TAG = "base-22_to_115"

_ENV = """\
export CI=true
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
  for (const result of suite.assertionResults || []) {
    const title = [file, ...(result.ancestorTitles || []), result.title].join(" > ").replace(/\\s+/g, " ").trim();
    const status = statusMap[result.status] || "SKIPPED";
    console.log("JEST_RESULT " + status + " " + title);
  }
}
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
cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

BASE_DATE=$(git show -s --format=%cI HEAD)
test -n "$BASE_DATE"
node --version
npm --version
npm install --before="$BASE_DATE" --legacy-peer-deps --no-package-lock

test -f /home/jest_report.js

npm test -- --ci --listTests > /home/list-tests.log 2>&1 || { tail -n 80 /home/list-tests.log; exit 1; }
tail -n 5 /home/list-tests.log
grep -qE "[.]js$" /home/list-tests.log

node -e "require.resolve('jest'); require.resolve('eslint'); require('./package.json'); console.log('DEPS_OK', process.version)"
"""

_STAGE = """\
#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
__APPLY__
if ! git diff --quiet HEAD -- package.json; then
  npm install --before="$(git show -s --format=%cI HEAD)" --legacy-peer-deps --no-package-lock
fi
rm -f /home/jest-results.json
STATUS=0
npm test -- --ci --json --outputFile=/home/jest-results.json || STATUS=$?
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
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__ENV__", _ENV)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__REPO__", pr.repo)
    )


class EslintPluginJestDom22To115ImageBase(Image):
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
            "        git ca-certificates build-essential python3 \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "RUN git config --global --add safe.directory '*'\n\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
            f"    cd /home/{repo} && git rev-parse HEAD >/dev/null\n\n"
            'CMD ["/bin/bash"]\n'
        )


class EslintPluginJestDom22To115ImageDefault(Image):
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
        return EslintPluginJestDom22To115ImageBase(self.pr, self._config)

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


@Instance.register("testing-library", "eslint_plugin_jest_dom_22_to_115")
class ESLINT_PLUGIN_JEST_DOM_22_TO_115(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return EslintPluginJestDom22To115ImageDefault(self.pr, self._config)

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

        for raw_line in _ANSI_RE.sub("", test_log).split("\n"):
            match = _RESULT_RE.match(raw_line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
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
