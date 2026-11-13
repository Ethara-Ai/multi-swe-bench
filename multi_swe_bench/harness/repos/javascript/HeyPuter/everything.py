"""HeyPuter/everything repo config.

The repo is a collection of independent apps: each folder in src/ with a
package.json has its own lockfile and test suite (see .github/workflows/ci.yml,
which runs `npm ci` + `npx vitest run` in src/<app> on Node 22). The PRs in the
dataset touch the `documents` app, so the config is scoped to src/documents.

Layout (per-PR base):
  * ImageBase   -- default Image.dockerfile(): node image, clone, pin BASE_COMMIT,
                   history scrub (Image._HARDENING_BLOCK).
  * ImageDefault -- copies the patches and scripts and runs prepare.sh
                   (pin check, `npm ci` in src/documents, hard gate).

Test command: the app's own vitest suite (`vitest run`, config in
src/documents/vite.config.js: environment node, tests/**/*.test.js; the
Playwright browser specs, tests/browser/*.spec.js, are not part of it).
Results are written with vitest's JSON reporter and printed one per line by
/home/vitest_results.cjs as `MSWE_TEST <STATUS> <file> > <describe...> > <test>`,
so describe context and the file are part of every test name. A test file that
fails to load is reported as `MSWE_TEST FAIL <file>`.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22.23.4-bookworm"
APP_DIR = "src/documents"
RESULTS_JSON = "/tmp/mswe-vitest-results.json"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export npm_config_audit=false\n"
    "export npm_config_fund=false\n"
    "export npm_config_update_notifier=false\n"
)

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# Prints vitest's JSON report one result per line. Fails (non-zero) if the
# report is missing or unreadable, so a runner that never started is loud.
_VITEST_RESULTS = r"""'use strict';
const fs = require('fs');
const path = require('path');

const [, , reportPath, rootDir] = process.argv;
const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const clean = s => String(s).replace(/\s+/g, ' ').trim();
const STATUS = { passed: 'PASS', failed: 'FAIL', skipped: 'SKIP', pending: 'SKIP', todo: 'SKIP', disabled: 'SKIP' };

console.log('MSWE_RESULTS_BEGIN');
for (const file of report.testResults || []) {
  const rel = path.relative(rootDir, file.name).split(path.sep).join('/');
  const tests = file.assertionResults || [];
  if (file.status === 'failed' && tests.length === 0) {
    // The file failed to load (syntax/import error); none of its tests ran.
    console.log(`MSWE_TEST FAIL ${rel}`);
    continue;
  }
  for (const t of tests) {
    const status = STATUS[t.status] || 'FAIL';
    const name = [rel, ...(t.ancestorTitles || []), t.title].map(clean).join(' > ');
    console.log(`MSWE_TEST ${status} ${name}`);
  }
}
console.log(`MSWE_RESULTS_END total=${report.numTotalTests} passed=${report.numPassedTests} failed=${report.numFailedTests} failedSuites=${report.numFailedTestSuites}`);
"""


def _npm_ci_with_retry() -> str:
    return (
        "ok=0\n"
        "for attempt in 1 2 3; do\n"
        "    if npm ci; then ok=1; break; fi\n"
        '    echo "npm ci attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        'test "$ok" = 1\n'
    )


def _test_block() -> str:
    # Same block in run.sh, test-run.sh and fix-run.sh. Runs from the repo root.
    return (
        f"cd {APP_DIR}\n"
        "# Re-sync node_modules only if a patch changed the dependency manifests.\n"
        "if ! git diff --quiet HEAD -- package.json package-lock.json; then\n"
        "    echo 'MSWE_LOG dependency manifests changed; running npm ci'\n"
        "    " + _npm_ci_with_retry().rstrip("\n").replace("\n", "\n    ") + "\n"
        "fi\n"
        f"rm -f {RESULTS_JSON}\n"
        "rc=0\n"
        "npx vitest run --reporter=dot --reporter=json "
        f"--outputFile.json={RESULTS_JSON} || rc=$?\n"
        'echo "MSWE_LOG vitest exit=$rc"\n'
        f"node /home/vitest_results.cjs {RESULTS_JSON} \"$PWD\"\n"
        "exit $rc\n"
    )


class EverythingImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []


class EverythingImageDefault(Image):
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
        return EverythingImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _header(self) -> str:
        return f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}\ncd /home/{self.pr.repo}\n\n"

    def _prepare_sh(self) -> str:
        sha = self.pr.base.sha
        return (
            f"{self._header()}"
            "# --- Section 1: pin ---\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            f"test -f {APP_DIR}/package-lock.json\n"
            "\n"
            "# --- Section 2: provision ---\n"
            f"cd {APP_DIR}\n"
            "node --version\n"
            "npm --version\n"
            f"{_npm_ci_with_retry()}"
            "\n"
            "# --- Section 3: gate ---\n"
            "test -x node_modules/.bin/vitest\n"
            "npx vitest --version\n"
            "node -e \"require.resolve('jsdom'); require.resolve('fflate'); require.resolve('yjs');\"\n"
            "npx vitest list --filesOnly > /tmp/mswe-vitest-files.txt\n"
            "test \"$(grep -c '\\.test\\.js' /tmp/mswe-vitest-files.txt)\" -gt 0\n"
            "rm -f /tmp/mswe-vitest-files.txt\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{self._header()}{_test_block()}"

    def _test_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_block()}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{self._header()}"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_block()}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "vitest_results.cjs", _VITEST_RESULTS),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("HeyPuter", "everything")
class EVERYTHING(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EverythingImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        log = log.replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        test_re = re.compile(r"^MSWE_TEST (PASS|FAIL|SKIP) (.+?)\s*$")

        in_results = False
        for line in log.splitlines():
            if line.startswith("MSWE_RESULTS_BEGIN"):
                in_results = True
                continue
            if line.startswith("MSWE_RESULTS_END"):
                in_results = False
                continue
            if not in_results:
                continue
            m = test_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "FAIL":
                failed_tests.add(name)
            elif status == "SKIP":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
