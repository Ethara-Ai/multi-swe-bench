"""garrytan/gstack: Claude Code skills + headless browser, a single bun package.

Toolchain (identical at the base.sha of every PR in the dataset, #3802..#3946):
  * bun 1.4.2: `engines.bun: ">=1.4.2"`; every workflow pins `bun-version: 1.4.2`.
  * lockfile: `bun.lock` (text). CI installs with
    `bun install --frozen-lockfile --ignore-scripts`.
  * tests: `bun test` (bunfig.toml preloads ./test-setup.ts). The full free
    suite is thousands of files sharded across 10 CI machines, so the graded run
    is scoped to the `*.test.ts` files touched by the PR's test.patch.

Per-PR base: the base pins BASE_COMMIT and scrubs history (enhanced by the
DockerfileEnhancer, no syntax directive); the PR layer only provisions.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_BUN_VERSION = "1.4.2"
_TEST_TIMEOUT_MS = "60000"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_TEST_FILE = re.compile(r"^(?:[^/]+/)*[^/]+\.test\.(?:c|m)?[jt]sx?$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT = re.compile(
    r"^GSTACK_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>\S+::.+)$"
)

_RUN_ENV = """export CI=true
export TZ=UTC
export NO_COLOR=1
export FORCE_COLOR=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_update_notifier=false
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

# Turns bun's JUnit report into one stable line per test case:
#   GSTACK_RESULT <PASSED|FAILED|SKIPPED> <file>::<describe> > ... > <test>
# The name carries the file and the full describe chain and never any timing.
_JUNIT_RESULTS = r"""const fs = require("fs");

const [xmlPath] = process.argv.slice(2);
if (!xmlPath || !fs.existsSync(xmlPath)) {
  process.exit(0);
}
const xml = fs.readFileSync(xmlPath, "utf8");

const decode = (s) =>
  s
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");

const attr = (tag, name) => {
  const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
  return m ? decode(m[1]) : "";
};

const clean = (s) => s.replace(/\s+/g, " ").trim();

const suites = [];
const seen = new Map();
let current = null;

const emit = (tc) => {
  const path = [...suites.slice(1), tc.name].map(clean).filter(Boolean).join(" > ");
  const key = `${tc.file}::${path}`;
  const n = (seen.get(key) || 0) + 1;
  seen.set(key, n);
  const name = n === 1 ? key : `${key} [${n}]`;
  process.stdout.write(`GSTACK_RESULT ${tc.status} ${name}\n`);
};

const tokens = xml.match(/<testsuite\b[^>]*>|<\/testsuite>|<testcase\b[^>]*>|<\/testcase>|<failure\b|<error\b|<skipped\b/g) || [];
for (const token of tokens) {
  if (token.startsWith("<testsuite")) {
    if (!token.endsWith("/>")) {
      suites.push(attr(token, "name"));
    }
  } else if (token === "</testsuite>") {
    suites.pop();
  } else if (token.startsWith("<testcase")) {
    const tc = { name: attr(token, "name"), file: attr(token, "file"), status: "PASSED" };
    if (token.endsWith("/>")) {
      emit(tc);
    } else {
      current = tc;
    }
  } else if (token === "</testcase>") {
    if (current) {
      emit(current);
    }
    current = null;
  } else if (current && (token === "<failure" || token === "<error")) {
    current.status = "FAILED";
  } else if (current && token === "<skipped" && current.status !== "FAILED") {
    current.status = "SKIPPED";
  }
}
"""

# Runs the given test files (those that exist in the current tree) with bun
# from the repo root, the way CI does, then emits one result line per case.
# With no existing file it runs nothing: a bare `bun test` would run the whole
# multi-thousand-file suite.
_RUN_TESTS = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

FILES=()
for f in "$@"; do
    if [ -f "$f" ]; then
        FILES+=("./$f")
    else
        echo "run-tests: $f not present in this tree, skipping"
    fi
done

if [ "${{#FILES[@]}}" -eq 0 ]; then
    echo "run-tests: no test files to run"
    exit 0
fi

OUT=/tmp/gstack-junit.xml
rm -f "$OUT"
STATUS=0
bun test --timeout {timeout} --reporter=junit --reporter-outfile="$OUT" "${{FILES[@]}}" || STATUS=$?
if [ ! -s "$OUT" ]; then
    echo "run-tests: bun test wrote no JUnit report (exit $STATUS)"
    exit 1
fi
bun /home/junit-results.js "$OUT"
exit $STATUS
"""


def _patch_files(patch: str) -> list[str]:
    files: list[str] = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _test_files(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _TEST_FILE.match(f)]


def _test_command(pr: PullRequest) -> str:
    return "bash /home/run-tests.sh " + " ".join(_test_files(pr)) + "\n"


class GstackImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        hardening = Image._HARDENING_BLOCK.rstrip("\n")
        # No syntax directive: this per-PR base is enhanced (ARGs, proxy/CA ENV,
        # cert farm, labels are injected ahead of the first RUN). The pin and
        # scrub are written out here, so the enhancer adds no second copy.
        return f"""FROM {_NODE_IMAGE}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git jq unzip python3 \\
    && rm -rf /var/lib/apt/lists/*

# bun from npm: the `bun` package selects @oven/bun-linux-x64 or
# @oven/bun-linux-aarch64 for the build platform, so no arch is hardcoded.
RUN npm install -g --no-audit --no-fund bun@{_BUN_VERSION} \\
    && test "$(bun --version)" = "{_BUN_VERSION}"

RUN git config --global --add safe.directory '*' && \\
    git config --global user.email "test@example.com" && \\
    git config --global user.name "test" && \\
    git config --global init.defaultBranch main

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GstackImageDefault(Image):
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
        return GstackImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_command = _test_command(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "junit-results.js", _JUNIT_RESULTS),
            File(
                ".",
                "run-tests.sh",
                _RUN_TESTS.format(repo=repo, timeout=_TEST_TIMEOUT_MS),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

# Section 2: provision (the commit's own bun.lock, as CI installs it)
{run_env}
bun install --frozen-lockfile --ignore-scripts

# Section 3: gate
test "$(bun --version)" = "{bun_version}"
test "$(git rev-parse HEAD)" = "{base_sha}"
bun -e "for (const m of ['diff', 'marked', 'cross-spawn', 'playwright/package.json', '@types/bun/package.json']) require.resolve(m); console.log('DEPS_OK', Bun.version)"
""".format(
                    repo=repo,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    bun_version=_BUN_VERSION,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch

{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("garrytan", "gstack")
class Gstack(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GstackImageDefault(self.pr, self._config)

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

        for raw in _ANSI.sub("", log).splitlines():
            match = _RESULT.match(raw.strip())
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

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
