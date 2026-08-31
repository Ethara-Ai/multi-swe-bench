import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Markers that fence the jest --json report inside the container log. parse_log
# slices between them instead of scraping the reporter, so test ids are taken
# from structured data whenever the report was written at all.
JSON_START = "===JEST_JSON_START==="
JSON_END = "===JEST_JSON_END==="

# Absolute clone path inside the image. jest's JSON report gives absolute suite
# paths; stripping this prefix yields repo-relative ids that are identical in
# all three stages.
REPO_ROOT = "/home/prom-client/"


class PromClientImageBase(Image):
    """Repo-level base: node 12 + a clone of siimon/prom-client.

    `package.json` at the base commit declares `engines.node: ">=10"` and pins
    `jest@^25.1.0`; `.travis.yml` at the same commit tests on node 10, 12 and
    latest. Node 12 is the era-correct choice and is the lowest version on
    which the PR under test actually exercises its new code path --
    `perf_hooks.monitorEventLoopDelay()` only exists from node 11.10 onward.

    `node:12-buster` is pinned deliberately and this Dockerfile runs **no**
    apt-get: Debian 10's repos are archived, so any `apt-get update` here would
    fail. It does not need one -- the full (non-slim) node image already ships
    git, curl, wget, ca-certificates, coreutils and build-essential, which is
    everything the build and the scripts use.

    `dependency()` returns a str, which is what makes DockerfileEnhancer
    rewrite the clone into the parameterized `${REPO_URL}` fetch plus
    `git checkout ${BASE_COMMIT}` and the history-hardening pass. That
    generated block terminates the file, so dependency install belongs in the
    per-PR prepare.sh, not here.
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

    def dependency(self) -> str | Image:
        return "node:12-buster"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV LC_ALL=C.UTF-8
ENV CI=true
ENV NPM_CONFIG_FUND=false
ENV NPM_CONFIG_AUDIT=false

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class PromClientImageDefault(Image):
    """PR-specific image: FROM the hardened base, add patches + scripts + install."""

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
        return PromClientImageBase(self.pr, self._config)

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash

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
                "run_tests.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}

# Only the unit suite. `npm test` also runs eslint + tsc, whose output is not
# test results and whose failures would mask the graded suite.
REPORT=/home/jest-report.json
rm -f "$REPORT"

# jest exits non-zero whenever a graded test fails, which is the expected state
# in the run and test-patch stages. The `if !` guard lets that through without
# `|| true`, so a jest that never STARTS still leaves an empty report and the
# missing-report branch below reports it loudly instead of silently yielding
# a 0/0/0 TestResult.
if ! timeout -k 30 900 ./node_modules/.bin/jest \\
        --ci --verbose --runInBand --json --outputFile="$REPORT"; then
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
                "prepare.sh",
                """\
#!/bin/bash
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

# No lockfile exists at this commit, so `npm install` is the repo's own install
# path. `|| true` is required: optional/native postinstall steps (husky) are
# allowed to fail without failing the image build.
npm install --no-audit --no-fund --unsafe-perm || true

# ...but a genuinely broken install must fail HERE, loudly, rather than later
# as an empty test log that parse_log turns into 0/0/0.
test -x node_modules/.bin/jest
node -e "console.log('prepare: node ' + process.version)"
node -e "console.log('prepare: jest ' + require('jest/package.json').version)"
""".format(repo=self.pr.repo, org=self.pr.org, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{repo}
bash /home/run_tests.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
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

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("siimon", "prom-client")
class PromClient(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PromClientImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Prefer jest's --json report; fall back to the verbose reporter.

        Both paths build the SAME id shape -- `path/to/suite.js::describe > test`
        -- because a stage that fell back to the reporter must still produce ids
        comparable with a stage that read the JSON. Different shapes across
        stages would make every test look new and manufacture a transition.

        No timing, duration or count metadata is ever folded into an id: the
        JSON path never sees any, and the reporter path strips the trailing
        `(N ms)` that jest appends to slow tests.
        """
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        seen: dict[str, int] = {}

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
                    name = self._disambiguate(name, seen)
                    status = assertion.get("status")
                    if status == "passed":
                        passed_tests.add(name)
                    elif status == "failed":
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)

                # A suite that died before any assertion ran (require error,
                # syntax error) reports zero assertions. Record the suite itself
                # as failed so the stage is not silently empty.
                if not assertions and suite.get("status") == "failed":
                    failed_tests.add(path or "unknown-suite")
        else:
            self._parse_verbose(
                clean_log, passed_tests, failed_tests, skipped_tests, seen
            )

        # TestResult.__post_init__ rejects overlapping sets. A retried or
        # duplicated test must land in exactly one bucket, failure winning.
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
    def _disambiguate(name: str, seen: dict[str, int]) -> str:
        """Make repeated ids unique so two tests never collapse into one.

        prom-client declares several sibling `it()` blocks carrying the SAME
        title inside the same `describe()` -- `test/counterTest.js` has two
        `should throw error if label lengths does not match` at identical
        nesting (lines 254 and 261 at the base commit). Their fully-qualified
        ids are byte-identical, so a plain `set` silently merges them and the
        suite reports one fewer test than jest ran.

        Tracking more `describe()` context cannot separate them -- they are
        siblings -- so repeats are suffixed with their occurrence ordinal. The
        FIRST occurrence keeps its bare id, which means a test that is not
        duplicated never changes shape. The ordinal is stable across the three
        stages because jest walks a suite in source order under `--runInBand`,
        and neither patch in this PR touches a file containing duplicates.
        """
        seen[name] = seen.get(name, 0) + 1
        count = seen[name]
        return name if count == 1 else f"{name} #{count}"

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
        seen: dict[str, int],
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
                name = PromClient._disambiguate(name, seen)
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
