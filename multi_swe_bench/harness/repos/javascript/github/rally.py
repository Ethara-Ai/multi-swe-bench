import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# github/rally PR #41 ("Rename probot-rally to rally"). A Probot GitHub-bot app:
# single npm package (NOT a monorepo), Jest ^25.2.7, no build step (plain JS).
# package.json carries an inline `jest` config and `"test": "jest"`; the suite
# (test/RallyValidate.test.js) mocks the robot/context/rally client directly, so
# it runs fully offline -- no network at test time (network only for npm install).
#
# We run jest with `--json --outputFile` and parse that report for per-test ids
# (path::describe > test), with a verbose-reporter fallback so a stage that fails
# to write JSON still yields ids comparable across the 3 stages.

JSON_START = "===JEST_JSON_START==="
JSON_END = "===JEST_JSON_END==="

REPO_ROOT = "/home/rally/"


class RallyImageBase(Image):
    """Repo base: node 14 + a hardened clone of github/rally.

    package.json pins jest@^25.2.7 (a 2020 stack) with engines node>=12, so a
    very new node cannot reliably run this suite. node:14 is the era-appropriate
    LTS that jest 25 supports; the full node image already ships git + npm, so no
    apt is needed (and none is run).

    dependency() returns a str, which makes DockerfileEnhancer generate the
    clone + `git checkout ${BASE_COMMIT}` + history-hardening pass; dependency
    install therefore lives in the per-PR prepare.sh, not here.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "node:14"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8

{code}

{self.clear_env}

"""


class RallyImageDefault(Image):
    """PR image: FROM the hardened base, add patches + scripts + install."""

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
        return RallyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

if [[ -n $(git status --porcelain -uno) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain -uno
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "patch_lib.sh",
                """#!/bin/bash

apply_patch() {
    local patch_file="$1"

    if git apply --whitespace=nowarn "$patch_file"; then
        echo "apply_patch: applied ${patch_file}"
        return 0
    fi

    echo "apply_patch: clean apply of ${patch_file} FAILED; re-running with --reject for diagnostics" >&2
    git apply --whitespace=nowarn --reject "$patch_file" || true

    echo "apply_patch: ---- rejected hunks ----" >&2
    find . -name '*.rej' -print -exec cat {} \\; >&2
    echo "apply_patch: ---- end rejected hunks ----" >&2
    echo "apply_patch: FATAL - ${patch_file} did not apply cleanly" >&2
    exit 1
}
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh

if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
fi
git checkout {sha}
bash /home/check_git_changes.sh

git remote remove origin 2>/dev/null || true
test -z "$(git remote)"

# npm package (package-lock.json present). `|| true` keeps a transient install
# hiccup non-fatal; the gate below then fails the build if jest is missing.
npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

test -x node_modules/.bin/jest
node -e "console.log('prepare: jest ' + require('jest/package.json').version)"
""".format(repo=self.pr.repo, org=self.pr.org, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}

REPORT=/home/jest-report.json
rm -f "$REPORT"

if ! timeout -k 30 900 npx --no-install jest --verbose --ci --json --outputFile="$REPORT"; then
    echo "run_tests: jest exited non-zero (expected when graded tests fail)"
fi

echo "{json_start}"
if [ -s "$REPORT" ]; then
    cat "$REPORT"
else
    echo "run_tests: NO JEST REPORT WRITTEN - the suite never ran" >&2
fi
echo "{json_end}"
""".format(repo=self.pr.repo, json_start=JSON_START, json_end=JSON_END),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
source /home/patch_lib.sh

apply_patch /home/test.patch

bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
source /home/patch_lib.sh

apply_patch /home/test.patch
apply_patch /home/fix.patch

bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("github", "rally")
class Rally(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RallyImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Prefer jest's --json report; fall back to the verbose reporter.

        Both paths build the SAME id shape -- `path/to/suite.js::describe > test`
        -- so a stage that fell back to the reporter still produces ids
        comparable with a stage that read the JSON (else every test would look
        new and manufacture a transition).
        """
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        payload = None
        start = clean_log.find(JSON_START)
        end = clean_log.rfind(JSON_END)
        if start != -1 and end > start:
            raw = clean_log[start + len(JSON_START) : end].strip()
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = None

        if isinstance(payload, dict):
            for suite in payload.get("testResults") or []:
                path = self._suite_path(suite.get("name") or "")
                assertions = suite.get("assertionResults") or []

                for assertion in assertions:
                    ancestors = [
                        part
                        for part in (assertion.get("ancestorTitles") or [])
                        if part
                    ]
                    title = " > ".join(ancestors + [assertion.get("title") or ""])
                    name = f"{path}::{title}" if path else title
                    status = assertion.get("status")
                    if status == "passed":
                        passed_tests.add(name)
                    elif status == "failed":
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)

                if not assertions and suite.get("status") == "failed":
                    failed_tests.add(path or "unknown-suite")
        else:
            self._parse_verbose(clean_log, passed_tests, failed_tests, skipped_tests)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    @staticmethod
    def _suite_path(name: str) -> str:
        path = (name or "").replace("\\", "/").strip()
        if path.startswith(REPO_ROOT):
            path = path[len(REPO_ROOT) :]
        return path

    @staticmethod
    def _parse_verbose(
        clean_log: str,
        passed_tests: set[str],
        failed_tests: set[str],
        skipped_tests: set[str],
    ) -> None:
        """Fallback for when no JSON report reached the log."""
        re_suite = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.jsx?)\s*(?:\(.*\))?\s*$")
        re_test = re.compile(
            r"^(\s*)([✓✔√✕✖×○✎])\s+"
            r"(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$"
        )
        re_describe = re.compile(r"^(\s+)([^\s●].*?)\s*$")
        re_skip_summary = re.compile(r"^skipped \d+ tests?$")

        passed_symbols = "✓✔√"
        failed_symbols = "✕✖×"

        current_file = ""
        ancestors: list[tuple[int, str]] = []

        for line in clean_log.splitlines():
            suite_match = re_suite.match(line)
            if suite_match:
                current_file = suite_match.group(2)
                ancestors = []
                continue

            test_match = re_test.match(line)
            if test_match:
                indent = len(test_match.group(1))
                symbol = test_match.group(2)
                title = test_match.group(3).strip()
                if re_skip_summary.match(title):
                    continue
                path = [t for i, t in ancestors if i < indent]
                full = " > ".join(path + [title])
                name = f"{current_file}::{full}" if current_file else full
                if symbol in passed_symbols:
                    passed_tests.add(name)
                elif symbol in failed_symbols:
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            if not current_file:
                continue

            describe_match = re_describe.match(line)
            if describe_match and len(describe_match.group(2)) < 200:
                indent = len(describe_match.group(1))
                ancestors = [(i, t) for i, t in ancestors if i < indent]
                ancestors.append((indent, describe_match.group(2).strip()))
