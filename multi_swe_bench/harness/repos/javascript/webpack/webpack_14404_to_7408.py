from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
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

_NODE_IMAGE = "node:16.20.2-bookworm"
_YARN_VERSION = "1.22.22"

_ROOT = f"/home/{_REPO}"

_JEST = (
    "node --expose-gc --max-old-space-size=4096 --experimental-vm-modules "
    "--trace-deprecation node_modules/jest-cli/bin/jest "
    "--ci --silent --passWithNoTests --maxWorkers=2"
)
_JEST_TIMEOUT = 3600

_LINE_TAG = "##JEST##"

_CASE_KINDS = {
    "cases": r'[/.]TestCases(\.template)?"',
    "configCases": r'[/.]ConfigTestCases(\.template)?"|"configCases"',
    "watchCases": r'[/.]WatchTestCases(\.template)?"|"watchCases"',
    "hotCases": r'[/.]HotTestCases(\.template)?"|"hotCases"',
    "statsCases": r'"statsCases"',
    "memoryLimitCases": r'"memoryLimitCases"',
}

_TEST_FILE = re.compile(
    r"^test/(?:.*/)?[^/]+\.(?:unittest|test|basictest|longtest|spectest)\.js$"
)
_SNAPSHOT = re.compile(r"^(test/(?:.*/)?)__snapshots__/([^/]+\.js)\.snap$")
_CATEGORISED_CASE = re.compile(
    r"^test/(cases|configCases|watchCases|hotCases)/([^/]+)/([^/]+)/"
)
_FLAT_CASE = re.compile(r"^test/(statsCases|memoryLimitCases)/([^/]+)/")
_DIFF_FILE = re.compile(r"^diff --git a/(\S+)", re.M)
_BIN_HUNK = re.compile(
    r"^diff --git a/(?P<old>\S+) b/(?P<new>\S+)\n(?P<body>(?:(?!diff --git ).*\n?)*)", re.M
)
_BIN_INDEX = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", re.M)
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
            selectors.setdefault(case.group(1), set()).add(
                f"{case.group(2)} {case.group(3)}"
            )
            continue
        flat = _FLAT_CASE.match(path)
        if flat:
            selectors.setdefault(flat.group(1), set()).add(flat.group(2))
    return {kind: sorted(names) for kind, names in selectors.items()}


def _binary_fixups(*patches: str) -> tuple[list[tuple[str, str]], list[str]]:
    restore: dict[str, str] = {}
    remove: set[str] = set()
    for patch in patches:
        for match in _BIN_HUNK.finditer(f"{patch}"):
            body = match.group("body")
            if "GIT binary patch" in body or "Binary files" not in body:
                continue
            index = _BIN_INDEX.search(body)
            if index is None:
                continue
            path = match.group("new")
            blob = index.group(2)
            if set(blob) == {"0"}:
                remove.add(path)
                restore.pop(path, None)
            else:
                restore[path] = blob
                remove.discard(path)
    return sorted(restore.items()), sorted(remove)


def _name_pattern(names: list[str]) -> str:
    escaped = "|".join(_JS_REGEX_SPECIAL.sub(r"\\\g<0>", n) for n in names)
    return "(^| )(" + escaped + ")( |$)"


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
	const crashed =
		suite.status === 'failed' ||
		String(suite.message || suite.failureMessage || '').trim() !== '';
	if (crashed && !results.some((r) => r.status === 'failed')) {
		emit('FAIL', `${file} > (suite)`);
	}
	for (const r of results) {
		if (r.status !== 'passed' && r.status !== 'failed') continue;
		emit(
			r.status === 'passed' ? 'PASS' : 'FAIL',
			[file, ...(r.ancestorTitles || []), r.title].join(' > ')
		);
	}
}
console.log(
	`${tag}_SUMMARY passed=${passed} failed=${failed} suites=${
		(report.testResults || []).length
	}`
);
"""

PREPARE_SH = """#!/bin/bash
set -e

echo "PREPARE: pin"
cd [[ROOT]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e [[SHA]]^{commit} 2>/dev/null \\
    || git fetch --no-tags origin [[SHA]]
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

echo "PREPARE: provision"
export CI=true
export HUSKY=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

node --version
yarn --version

yarn install --frozen-lockfile --ignore-engines --network-timeout 600000 \\
    || yarn install --frozen-lockfile --ignore-engines --network-timeout 600000 \\
    || true

yarn link || true
yarn link webpack || true

cat > /home/jest-lines.js <<'__JEST_LINES_EOF__'
[[JEST_LINES_JS]]__JEST_LINES_EOF__

echo "PREPARE: gate"
cd [[ROOT]]
test -f node_modules/jest-cli/bin/jest.js
test "$(readlink -f node_modules/webpack)" = "[[ROOT]]"
node -e 'require("webpack")'
node -e '
const fs = require("fs");
const pkg = require("./package.json");
const names = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
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
    grep -lE "$1" test/*.basictest.js test/*.test.js test/*.longtest.js 2>/dev/null \\
        | LC_ALL=C sort || true
}

run_cases() {
    local kind="$1" pattern="$2" runner_re="$3"
    mapfile -t runners < <(runners_for "$runner_re")
    if [ "${#runners[@]}" -eq 0 ]; then
        echo "Error: no runner file found for ${kind}" >&2
        exit 1
    fi
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
bash /home/run_tests.sh
"""

RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export NODE_ENV=test
export FORCE_COLOR=0

cd [[ROOT]]
[[HELPERS]]

[[COMMANDS]]
"""

RESTORE_HELPER = """restore_blob() {
    local path="$1" blob="$2"
    mkdir -p "$(dirname "$path")"
    if ! git cat-file -e "${blob}^{blob}" 2>/dev/null; then
        echo "Error: blob ${blob} for ${path} is absent from the base commit object store" >&2
        exit 1
    fi
    git cat-file blob "$blob" > "$path"
}

"""

APPLY_TEST = """[[HELPER]]if ! git apply --whitespace=nowarn[[EXCLUDES]] /home/test.patch; then
    echo "Error: git apply of test.patch failed" >&2
    exit 1
fi
[[FIXUPS]]"""

APPLY_FIX = """[[HELPER]]if ! git apply --whitespace=nowarn[[EXCLUDES]] /home/test.patch /home/fix.patch; then
    echo "Error: git apply of test.patch and fix.patch failed" >&2
    exit 1
fi
[[FIXUPS]]"""

FIX_DEPS_INSTALL = """
yarn install --frozen-lockfile --ignore-engines --prefer-offline --network-timeout 600000
yarn link webpack"""

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM [[NODE_IMAGE]]

ARG TARGETARCH
ARG REPO_URL="https://github.com/[[ORG_REPO]].git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_GLOBAL_CACHE=1 \\
    FORCE_COLOR=1 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true \\
    HUSKY=0 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

LABEL org.opencontainers.image.title="[[ORG_REPO]]" \\
      org.opencontainers.image.description="[[ORG_REPO]] Docker image" \\
      org.opencontainers.image.source="https://github.com/[[ORG_REPO]]" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare yarn@[[YARN_VERSION]] --activate

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" [[ROOT]] && \\
    cd [[ROOT]] && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""

PR_PIN = """RUN git reset --hard
RUN git checkout [[SHA]]"""

PR_HARDENING = """RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

PR_SUBMODULES = """RUN if [ -f [[ROOT]]/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""


class WebpackImageBase14404To7408(Image):
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
        return (
            BASE_DOCKERFILE.replace("[[NODE_IMAGE]]", _NODE_IMAGE)
            .replace("[[ORG_REPO]]", f"{_ORG}/{_REPO}")
            .replace("[[YARN_VERSION]]", _YARN_VERSION)
            .replace("[[ROOT]]", _ROOT)
        )


class WebpackImageDefault14404To7408(Image):
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
        return WebpackImageBase14404To7408(self.pr, self._config)

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
            .replace("[[JEST_LINES_JS]]", JEST_LINES_JS)
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
            raise ValueError(
                f"{self.pr.org}/{self.pr.repo}#{self.pr.number}: test.patch selects no tests"
            )
        return "\n".join(lines)

    def _patch_step(self, template: str, *patches: str) -> str:
        restore, remove = _binary_fixups(*patches)
        if not restore and not remove:
            return (
                template.replace("[[HELPER]]", "")
                .replace("[[EXCLUDES]]", "")
                .replace("[[FIXUPS]]", "")
                .rstrip("\n")
            )
        excludes = "".join(
            f" --exclude={shlex.quote(path)}" for path, _ in restore
        ) + "".join(f" --exclude={shlex.quote(path)}" for path in remove)
        fixups = [
            f"restore_blob {shlex.quote(path)} {shlex.quote(blob)}"
            for path, blob in restore
        ] + [f"rm -f {shlex.quote(path)}" for path in remove]
        return (
            template.replace("[[HELPER]]", RESTORE_HELPER)
            .replace("[[EXCLUDES]]", excludes)
            .replace("[[FIXUPS]]", "\n".join(fixups))
        )

    def _stage(self, patch_step: str) -> str:
        return self._expand(STAGE_SH.replace("[[PATCH_STEP]]", patch_step))

    def _run_tests(self) -> str:
        return self._expand(
            RUN_TESTS_SH.replace("[[HELPERS]]", RUN_HELPERS_SH).replace(
                "[[COMMANDS]]", self._commands()
            )
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
                self._expand(PREPARE_SH),
            ),
            File(".", "run_tests.sh", self._run_tests()),
            File(".", "run.sh", self._stage("")),
            File(
                ".",
                "test-run.sh",
                self._stage(self._patch_step(APPLY_TEST, self.pr.test_patch)),
            ),
            File(
                ".",
                "fix-run.sh",
                self._stage(
                    self._patch_step(APPLY_FIX, self.pr.test_patch, self.pr.fix_patch)
                    + (FIX_DEPS_INSTALL if moves_deps else "")
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        sha = self.pr.base.sha
        sections = [f"FROM {base.image_full_name()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f"WORKDIR {_ROOT}")
        sections.append(PR_PIN.replace("[[SHA]]", sha))
        sections.append(
            "".join(f"COPY {f.name} /home/\n" for f in self.files()).rstrip("\n")
        )
        sections.append("RUN bash /home/prepare.sh")
        sections.append(PR_HARDENING.replace("[[SHA]]", sha))
        sections.append(PR_SUBMODULES.replace("[[ROOT]]", _ROOT))
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
class WEBPACK_14404_TO_7408(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WebpackImageDefault14404To7408(self.pr, self._config)

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
            return WEBPACK_14404_TO_7408(pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_era_create)
    setattr(Instance, _HOOK_FLAG, True)
