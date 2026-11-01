from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "webpack"
_REPO = "webpack"

_ERA_KEY = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"webpack_(\d+)_to_(\d+)", _ERA_KEY)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like webpack_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

_NODE_IMAGE = "node:24.22.2-bookworm"

_ROOT = f"/home/{_REPO}"

_JEST = (
    "node --expose-gc --max-old-space-size=4096 --experimental-vm-modules "
    "--trace-deprecation node_modules/jest-cli/bin/jest "
    "--workerIdleMemoryLimit=512MB --ci --silent --passWithNoTests --maxWorkers=50%"
)
_JEST_TIMEOUT = 3600

_LINE_TAG = "##JEST##"

_CASE_KINDS = {
    "cases": r'[/.]TestCases(\.template)?"',
    "configCases": r'[/.]ConfigTestCases(\.template)?"|"configCases"',
    "watchCases": r'[/.]WatchTestCases(\.template)?"',
    "hotCases": r'[/.]HotTestCases(\.template)?"',
    "statsCases": r'"statsCases"',
}

_TEST_FILE = re.compile(r"^test/(?:.*/)?[^/]+\.(?:unittest|test|basictest|longtest|spectest)\.js$")
_SNAPSHOT = re.compile(r"^(test/(?:.*/)?)__snapshots__/([^/]+\.js)\.snap$")
_CATEGORISED_CASE = re.compile(r"^test/(cases|configCases|watchCases|hotCases)/([^/]+)/([^/]+)/")
_STATS_CASE = re.compile(r"^test/statsCases/([^/]+)/")
_DIFF_FILE = re.compile(r"^diff --git a/(\S+)", re.M)
_JS_REGEX_SPECIAL = re.compile(r"[.*+?^${}()|\[\]\\/]")

_BROWSER_TEST_FILES = {"test/specCases/syntaxEquivalence.spectest.js"}

_DEP_FILES = {"package.json", "yarn.lock"}


def _patched_files(pr: PullRequest) -> list[str]:
    return sorted(set(_DIFF_FILE.findall(f"{pr.test_patch}")))


def _fix_moves_deps(pr: PullRequest) -> bool:
    return bool(_DEP_FILES & set(_DIFF_FILE.findall(f"{pr.fix_patch}")))


def _direct_test_files(pr: PullRequest) -> list[str]:
    files = set()
    for path in _patched_files(pr):
        if path in _BROWSER_TEST_FILES:
            continue
        if _TEST_FILE.match(path):
            files.add(path)
            continue
        snapshot = _SNAPSHOT.match(path)
        if snapshot:
            files.add(snapshot.group(1) + snapshot.group(2))
    return sorted(files)


def _case_selectors(pr: PullRequest) -> dict[str, list[str]]:
    selectors: dict[str, set[str]] = {}
    for path in _patched_files(pr):
        case = _CATEGORISED_CASE.match(path)
        if case:
            selectors.setdefault(case.group(1), set()).add(f"{case.group(2)} {case.group(3)}")
            continue
        stats = _STATS_CASE.match(path)
        if stats:
            selectors.setdefault("statsCases", set()).add(stats.group(1))
    return {kind: sorted(names) for kind, names in selectors.items()}


def _name_pattern(names: list[str]) -> str:
    return "(^| )(" + "|".join(_JS_REGEX_SPECIAL.sub(r"\\\g<0>", n) for n in names) + ") "


CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -40
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

JEST_LINES_JS = r"""'use strict';
const fs = require('fs');
const path = require('path');

const [, , reportFile, root, tag] = process.argv;
const report = JSON.parse(fs.readFileSync(reportFile, 'utf8'));
const seen = new Map();
let passed = 0;
let failed = 0;

const emit = (status, name) => {
	const count = (seen.get(name) || 0) + 1;
	seen.set(name, count);
	console.log(`${tag} ${status} ${count > 1 ? `${name} #${count}` : name}`);
	if (status === 'PASS') passed++;
	else failed++;
};

for (const suite of report.testResults || []) {
	const file = path.relative(root, suite.name).split(path.sep).join('/');
	const results = suite.assertionResults || [];
	if (suite.status === 'failed' && !results.some((r) => r.status === 'failed')) {
		emit('FAIL', `${file} > (suite)`);
	}
	for (const r of results) {
		if (r.status !== 'passed' && r.status !== 'failed') continue;
		emit(r.status === 'passed' ? 'PASS' : 'FAIL', [file, ...r.ancestorTitles, r.title].join(' > '));
	}
}
console.log(`${tag}_SUMMARY passed=${passed} failed=${failed} suites=${(report.testResults || []).length}`);
"""

PREPARE_SH = """#!/bin/bash
set -e

# --- Section 1: PIN -------------------------------------------------------
cd [[ROOT]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e [[SHA]]^{commit} 2>/dev/null \\
    || git fetch --no-tags origin [[SHA]]
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# --- Section 2: PROVISION -------------------------------------------------
export CI=true
export HUSKY=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

node --version
yarn --version

# `|| true`: a failed install must not abort the image build here; the GATE
# section below fails the build if the install left the tree unusable.
yarn install --frozen-lockfile --network-timeout 600000 \\
    || yarn install --frozen-lockfile --network-timeout 600000 \\
    || true

# The suites require("webpack"); CI links the checkout into itself the same way.
yarn link || true
yarn link webpack || true

cat > /home/jest-lines.js <<'__JEST_LINES_EOF__'
[[JEST_LINES_JS]]__JEST_LINES_EOF__
[[FIX_DEPS_CACHE]]
# --- Section 3: GATE ------------------------------------------------------
cd [[ROOT]]
test -f node_modules/jest-cli/bin/jest.js
test "$(readlink -f node_modules/webpack)" = "[[ROOT]]"
node -e 'require("webpack")'
node -e '
const fs = require("fs");
const pkg = require("./package.json");
const names = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
// Checked on disk: several ESM-only deps do not export ./package.json.
const missing = names.filter((n) => !fs.existsSync("node_modules/" + n + "/package.json"));
if (missing.length) { console.error("missing deps: " + missing.join(" ")); process.exit(1); }
console.log("deps resolved: " + names.length);
'
node --check /home/jest-lines.js
listed=$(node node_modules/jest-cli/bin/jest --listTests | grep -c '\\.js$')
test "$listed" -gt 0
echo "DEPS_OK tests=$listed"
"""

RUN_HELPERS_SH = """run_jest() {
    local label="$1"; shift
    local out="/tmp/jest-${label}.json"
    rm -f "$out"
    echo "##SUITE## ${label}"
    local rc=0
    timeout -k 30 [[JEST_TIMEOUT]] [[JEST]] --json --outputFile="$out" "$@" || rc=$?
    if [ ! -s "$out" ]; then
        echo "Error: jest wrote no report for ${label} (exit ${rc})" >&2
        exit $(( rc == 0 ? 1 : rc ))
    fi
    node /home/jest-lines.js "$out" "[[ROOT]]" "[[TAG]]"
}

runners_for() {
    grep -lE "$1" test/*.basictest.js test/*.test.js test/*.longtest.js 2>/dev/null | LC_ALL=C sort || true
}

run_cases() {
    local kind="$1" pattern="$2" runner_re="$3"
    mapfile -t runners < <(runners_for "$runner_re")
    if [ "${#runners[@]}" -eq 0 ]; then
        echo "Error: no runner file found for ${kind}" >&2
        exit 1
    fi
    # One runner at a time: runners of the same case tree run each case's
    # test.filter.js while collecting, and copy/symlink's filter creates a fixed
    # probe symlink, so two runners in parallel workers race on it (EEXIST).
    local runner
    for runner in "${runners[@]}"; do
        run_jest "${kind}-$(basename "$runner" .js)" --runTestsByPath "$runner" -t "$pattern"
    done
}

run_files() {
    local present=()
    for f in "$@"; do
        [ -f "$f" ] && present+=("$f")
    done
    if [ "${#present[@]}" -gt 0 ]; then
        run_jest files --runTestsByPath "${present[@]}"
    else
        echo "##SUITE## files (none present at this stage)"
    fi
}"""

STAGE_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test
export FORCE_COLOR=0

cd [[ROOT]]
[[PATCH_STEP]]
[[HELPERS]]

[[COMMANDS]]
"""

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi"""

FIX_DEPS_CACHE = """
mkdir -p /tmp/fix-deps
cp package.json yarn.lock /tmp/fix-deps/
# .yarnrc carries `ignore-engines true`; without it Yarn Classic refuses @changesets/cli.
if [ -f .yarnrc ]; then cp .yarnrc /tmp/fix-deps/; fi
(
    cd /tmp/fix-deps
    git apply --include=package.json --include=yarn.lock /home/fix.patch
    yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000 \\
        || yarn install --frozen-lockfile --ignore-scripts --network-timeout 600000
    rm -rf node_modules
    yarn install --frozen-lockfile --ignore-scripts --offline
)
rm -rf /tmp/fix-deps
"""

FIX_DEPS_INSTALL = """
# The fix moves a dependency; install its lockfile from the build-time cache.
yarn install --frozen-lockfile --offline
yarn link webpack"""


class WebpackImageBase22869To21733(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {self.dependency()}",
            DockerfileEnhancer._infrastructure_block(self, self.dependency()).rstrip("\n"),
            "ENV CI=true \\\n    HUSKY=0 \\\n    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
            "RUN git config --global --add safe.directory '*'",
            "WORKDIR /home/",
            f'RUN git clone "${{REPO_URL}}" {_ROOT}',
            f"WORKDIR {_ROOT}",
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class WebpackImageDefault22869To21733(Image):
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
        return WebpackImageBase22869To21733(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[ROOT]]", _ROOT)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[JEST_TIMEOUT]]", str(_JEST_TIMEOUT))
            .replace("[[JEST]]", _JEST)
            .replace("[[TAG]]", _LINE_TAG)
        )

    def _commands(self) -> str:
        lines = []
        direct = _direct_test_files(self.pr)
        if direct:
            lines.append("run_files " + " ".join(shlex.quote(f) for f in direct))
        for kind, names in _case_selectors(self.pr).items():
            lines.append(
                f"run_cases {kind} {shlex.quote(_name_pattern(names))} "
                f"{shlex.quote(_CASE_KINDS[kind])}"
            )
        if not lines:
            raise ValueError(f"{self.pr.org}/{self.pr.repo}#{self.pr.number}: test.patch selects no tests")
        return "\n".join(lines)

    def _stage(self, patch_step: str) -> str:
        return self._expand(
            STAGE_SH.replace("[[PATCH_STEP]]", patch_step)
            .replace("[[HELPERS]]", RUN_HELPERS_SH)
            .replace("[[COMMANDS]]", self._commands())
        )

    def files(self) -> list[File]:
        moves_deps = _fix_moves_deps(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                self._expand(
                    PREPARE_SH.replace("[[JEST_LINES_JS]]", JEST_LINES_JS).replace(
                        "[[FIX_DEPS_CACHE]]", FIX_DEPS_CACHE if moves_deps else ""
                    )
                ),
            ),
            File(".", "run.sh", self._stage("")),
            File(".", "test-run.sh", self._stage(APPLY_TEST)),
            File(
                ".",
                "fix-run.sh",
                self._stage(APPLY_FIX + (FIX_DEPS_INSTALL if moves_deps else "")),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        sections = [f"FROM {base.image_full_name()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f'ARG BASE_COMMIT="{self.pr.base.sha}"')
        sections.append("".join(f"COPY {f.name} /home/\n" for f in self.files()).rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        sections.append(f"WORKDIR {_ROOT}")
        sections.append(Image._HARDENING_BLOCK.strip("\n"))
        if self.clear_env:
            sections.append(self.clear_env)
        return "\n\n".join(sections) + "\n"


_LINE_RE = re.compile(r"^" + re.escape(_LINE_TAG) + r" (PASS|FAIL) (.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def parse_webpack_log(log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for raw in _ANSI_RE.sub("", log).splitlines():
        match = _LINE_RE.match(raw.strip())
        if not match:
            continue
        if match.group(1) == "PASS":
            passed.add(match.group(2))
        else:
            failed.add(match.group(2))

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register(_ORG, _ERA_KEY)
class WEBPACK_22869_TO_21733(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebpackImageDefault22869To21733(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_webpack_log(test_log)


def _owns(pr: PullRequest) -> bool:
    if getattr(pr, "org", "") != _ORG or getattr(pr, "repo", "") != _REPO:
        return False
    try:
        number = int(getattr(pr, "number", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not _ERA_LO <= number <= _ERA_HI:
        return False
    interval = str(getattr(pr, "number_interval", "") or "")
    return interval in ("", _ERA_KEY)


_HOOK_FLAG = f"_{_ERA_KEY}_route_hook"

if not getattr(Instance, _HOOK_FLAG, False):
    _previous_create = Instance.create.__func__

    def _era_create(cls, pr, config, *args, **kwargs):
        if _owns(pr):
            return WEBPACK_22869_TO_21733(pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_era_create)
    setattr(Instance, _HOOK_FLAG, True)
