"""CapSoftware/Cap: screen recorder monorepo (Tauri/GPUI desktop app, web apps,
Rust crates). GitHub classifies it as Rust, so the config lives under `rust/`.

The dataset PRs' tests live in the repo's dependency-free Node packaging
scripts (`scripts/*.test.mjs`, run in CI with `node --test`, see the
"Test desktop build preparation" / "Test Linux packaging" steps of
.github/workflows/ci.yml). They import only `node:` built-ins and the sibling
`scripts/*.mjs` modules, so no package install, Rust toolchain, display or
GPU is needed.

Toolchain: Node 24 (`engines.node: ">=24"` in package.json at base.sha).

Graded files: the dependency-free script suites (below) plus any
`scripts/*.test.mjs` touched by the PR's test.patch. Excluded at base for
needing things the container does not have: bun-scripts.test.mjs (bun),
check-tauri-plugin-versions.test.mjs (workspace node_modules), setup.test.mjs
(network downloads).

Each file runs in its own `node --test` process with a custom reporter that
prints one stable line per test case:
    CAP_RESULT <PASSED|FAILED|SKIPPED> <file>::<parent> > ... > <test>
If a file cannot even load (test.patch imports exports only fix.patch adds),
the reporter marks FAILED exactly the tests test.patch adds or modifies in
that file, under the same names the fix stage reports.

Per-PR base: the base pins BASE_COMMIT and scrubs history (enhanced by the
DockerfileEnhancer, no syntax directive); the PR layer only provisions.
Run stages need nothing outside the tracked tree and /home helpers, so they
work from `git checkout -- . && git clean -fd` with no network.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_NODE_MAJOR = 24

_BASE_TEST_FILES = [
    "scripts/build-desktop-binaries-cache.test.mjs",
    "scripts/build-macos-packages.test.mjs",
    "scripts/bundle-gpui.test.mjs",
    "scripts/finalize-linux-appimage.test.mjs",
    "scripts/finalize-linux-gpui-packages.test.mjs",
    "scripts/finalize-macos-packages.test.mjs",
    "scripts/linux-bundle-config.test.mjs",
    "scripts/prepare-gpui-dependency.test.mjs",
    "scripts/run-gpui-build.test.mjs",
]

_DIFF_HEADER = re.compile(r"^diff --git a/\S+ b/(\S+)$")
_TEST_FILE = re.compile(r"^scripts/[^/]+\.test\.mjs$")
_HUNK = re.compile(r"^@@ [^@]* @@ ?(.*)$")
_TEST_START = re.compile(r"""^(?:test|it)\(\s*(["'])((?:\\.|(?!\1).)*)\1""")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT = re.compile(r"^CAP_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>\S+::.+)$")

_RUN_ENV = """export CI=true
export TZ=UTC
export NO_COLOR=1
export FORCE_COLOR=0
export NODE_DISABLE_COLORS=1
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

# node:test custom reporter: one stable line per test case, never timing.
_REPORTER = r"""import { readFileSync } from "node:fs";
import path from "node:path";

const file = process.env.CAP_TEST_FILE || "";
const expectedPath = process.env.CAP_TEST_EXPECTED || "";
const clean = (s) => String(s).replace(/\s+/g, " ").trim();

export default async function* reporter(source) {
  const stack = [];
  const seen = new Map();
  const results = [];
  const fileLevel = [];
  for await (const event of source) {
    const data = event.data || {};
    if (event.type === "test:start") {
      stack[data.nesting] = clean(data.name);
      stack.length = data.nesting + 1;
      continue;
    }
    if (event.type !== "test:pass" && event.type !== "test:fail") continue;
    const name = clean(data.name);
    const isFile =
      data.nesting === 0 &&
      data.file &&
      (path.resolve(name) === path.resolve(data.file) || name === file);
    let status = event.type === "test:fail" ? "FAILED" : "PASSED";
    if (status === "PASSED" && (data.skip !== undefined || data.todo !== undefined)) {
      status = "SKIPPED";
    }
    if (isFile) {
      if (status === "FAILED") fileLevel.push(name);
      continue;
    }
    const parts = [...stack.slice(0, data.nesting), name].filter(Boolean);
    const key = `${file}::${parts.join(" > ")}`;
    const n = (seen.get(key) || 0) + 1;
    seen.set(key, n);
    results.push([status, n === 1 ? key : `${key} [${n}]`]);
  }
  // The file failed before any test ran (e.g. a missing export): report the
  // tests test.patch adds/modifies here as failed, named as the fix stage names them.
  if (results.length === 0 && fileLevel.length > 0) {
    let expected = [];
    try {
      expected = readFileSync(expectedPath, "utf8").split("\n").map(clean).filter(Boolean);
    } catch {}
    if (expected.length === 0) expected = ["<file failed to load>"];
    for (const name of expected) results.push(["FAILED", `${file}::${name}`]);
  }
  for (const [status, name] of results) yield `CAP_RESULT ${status} ${name}\n`;
}
"""

# Runs each existing test file in its own node --test process so one file
# that fails to load cannot hide the others' results.
_RUN_TESTS = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

RAN=0
STATUS=0
for f in "$@"; do
    if [ ! -f "$f" ]; then
        echo "run-tests: $f not present in this tree, skipping"
        continue
    fi
    RAN=$((RAN + 1))
    EXPECTED="$(mktemp)"
    awk -F '\\t' -v f="$f" '$1 == f {{ print $2 }}' /home/expected-tests.tsv > "$EXPECTED"
    echo "run-tests: node --test $f"
    FILE_STATUS=0
    CAP_TEST_FILE="$f" CAP_TEST_EXPECTED="$EXPECTED" \\
        node --test --test-concurrency=1 --test-reporter=/home/node-test-results.mjs "$f" \\
        || FILE_STATUS=$?
    rm -f "$EXPECTED"
    if [ "$FILE_STATUS" -ne 0 ]; then
        echo "run-tests: $f exited $FILE_STATUS"
        STATUS=$FILE_STATUS
    fi
done

if [ "$RAN" -eq 0 ]; then
    echo "run-tests: no test files to run"
    exit 1
fi
exit $STATUS
"""


def _unescape_js(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def _test_start_name(content: str) -> Optional[str]:
    m = _TEST_START.match(content)
    return _unescape_js(m.group(2)) if m else None


def _patch_test_files(patch: str) -> list[str]:
    files: list[str] = []
    for line in (patch or "").splitlines():
        m = _DIFF_HEADER.match(line)
        if m and _TEST_FILE.match(m.group(1)) and m.group(1) not in files:
            files.append(m.group(1))
    return files


def _patch_touched_tests(patch: str) -> list[tuple[str, str]]:
    """Top-level node:test cases test.patch adds or edits, per test file."""
    touched: list[tuple[str, str]] = []
    current_file: Optional[str] = None
    current_test: Optional[str] = None

    def mark(name: Optional[str]) -> None:
        if current_file and name and (current_file, name) not in touched:
            touched.append((current_file, name))

    for line in (patch or "").splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            current_file = m.group(1) if _TEST_FILE.match(m.group(1)) else None
            current_test = None
            continue
        if current_file is None or line.startswith(("--- ", "+++ ")):
            continue
        h = _HUNK.match(line)
        if h:
            current_test = _test_start_name(h.group(1))
            continue
        if not line or line[0] not in " +-":
            continue
        kind, content = line[0], line[1:]
        started = _test_start_name(content)
        if started is not None:
            if kind == "-":
                continue
            current_test = started
            if kind == "+":
                mark(started)
            continue
        if kind in "+-":
            mark(current_test)
        if content.startswith("})") and kind != "-":
            current_test = None
    return touched


def _test_files(pr: PullRequest) -> list[str]:
    files = list(_BASE_TEST_FILES)
    for f in _patch_test_files(pr.test_patch):
        if f not in files:
            files.append(f)
    return files


def _test_command(pr: PullRequest) -> str:
    return "bash /home/run-tests.sh " + " ".join(_test_files(pr)) + "\n"


def _expected_tsv(pr: PullRequest) -> str:
    return "".join(f"{f}\t{name}\n" for f, name in _patch_touched_tests(pr.test_patch))


class CapImageBase(Image):
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
        ca-certificates git binutils \\
    && rm -rf /var/lib/apt/lists/*

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


class CapImageDefault(Image):
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
        return CapImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_command = _test_command(self.pr)
        check_files = " ".join(_test_files(self.pr)[: len(_BASE_TEST_FILES)])
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "node-test-results.mjs", _REPORTER),
            File(".", "expected-tests.tsv", _expected_tsv(self.pr)),
            File(".", "run-tests.sh", _RUN_TESTS.format(repo=repo)),
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

# Section 2: provision. The graded suites import only node: built-ins and
# sibling scripts/*.mjs modules, so there is nothing to install.
{run_env}

# Section 3: gate
test "$(git rev-parse HEAD)" = "{base_sha}"
test "$(node -p 'process.versions.node.split(".")[0]')" -ge {node_major}
for f in {check_files}; do
    test -f "$f"
    node --check "$f"
done
node --input-type=module -e "await import('node:test'); await import('/home/node-test-results.mjs'); console.log('DEPS_OK', process.version)"
""".format(
                    repo=repo,
                    base_sha=self.pr.base.sha,
                    run_env=_RUN_ENV,
                    node_major=_NODE_MAJOR,
                    check_files=check_files,
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


@Instance.register("CapSoftware", "Cap")
class Cap(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CapImageDefault(self.pr, self._config)

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
