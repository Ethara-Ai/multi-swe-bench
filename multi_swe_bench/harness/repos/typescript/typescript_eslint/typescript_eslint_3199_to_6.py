from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "typescript_eslint_3199_to_6"
_BASE_IMAGE = "node:12.22.12-bullseye"
_BASE_TAG = "base-3199_to_6"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_PKG_FILE_RE = re.compile(r"^packages/(?P<pkg>[^/]+)/(?P<rel>.+)$")
_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.[jt]sx?$")
_LIB_TEST_RE = re.compile(r"^tests/lib/.+\.[jt]sx?$")
_RESULT_RE = re.compile(r"@@TSE@@\t(PASS|FAIL|SKIP)\t(.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_REPORTER_JS = r"""'use strict';
const path = require('path');
class MsweReporter {
  constructor() {
    this.counts = new Map();
  }
  emit(status, name) {
    process.stdout.write('\n@@TSE@@\t' + status + '\t' + name + '\n');
  }
  unique(base) {
    const n = (this.counts.get(base) || 0) + 1;
    this.counts.set(base, n);
    return n > 1 ? base + ' #' + n : base;
  }
  onTestResult(test, result) {
    const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
    const rel = path.relative(process.cwd(), result.testFilePath).split(path.sep).join('/');
    const file = (process.env.MSWE_PKG || '.') + '/' + rel;
    const items = result.testResults || [];
    if (result.testExecError || (result.failureMessage && items.length === 0)) {
      this.emit('FAIL', this.unique(file + ' > (suite)'));
    }
    for (const a of items) {
      const name = this.unique([file].concat(a.ancestorTitles || [], [a.title]).map(clean).join(' > '));
      if (a.status === 'passed') {
        this.emit('PASS', name);
      } else if (a.status === 'failed') {
        this.emit('FAIL', name);
      } else {
        this.emit('SKIP', name);
      }
    }
  }
}
module.exports = MsweReporter;
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_ENV_SH = """export CI=true
export NODE_ENV=test
export HUSKY=0
export HUSKY_SKIP_INSTALL=1
export HUSKY_SKIP_HOOKS=1
export OPENCOLLECTIVE_HIDE=1
export DISABLE_OPENCOLLECTIVE=1
export ADBLOCK=1
export NODE_OPTIONS=--max-old-space-size=4096

build_all() {
    local TSC_BIN="$PWD/node_modules/.bin/tsc"
    node_modules/.bin/lerna run build < /dev/null && return 0
    for d in packages/*/; do
        node -e "const p = require('./${d}package.json'); process.exit(p.scripts && p.scripts.build ? 0 : 1)" 2>/dev/null || continue
        (cd "$d" && yarn build < /dev/null) && continue
        [ -f "${d}tsconfig.build.json" ] || continue
        (cd "$d" && "$TSC_BIN" -p tsconfig.build.json < /dev/null) || true
    done
}
"""

_PREPARE_SH = """#!/bin/bash
set -e

write_reporter() {
    cat > /home/mswe_reporter.js <<'MSWE_REPORTER_EOF'
__REPORTER__MSWE_REPORTER_EOF
}

write_reporter

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__ENV__
__DEPS__
yarn install --frozen-lockfile --ignore-engines --network-timeout 600000 \\
    || yarn install --frozen-lockfile --ignore-engines --ignore-scripts --network-timeout 600000 \\
    || yarn install --ignore-engines --ignore-scripts --network-timeout 600000 \\
    || true
git checkout -- .
git clean -fd
build_all || true
yarn cache clean || true

node -e "['jest', 'ts-jest', 'typescript'].forEach((m) => require.resolve(m)); if (!require('fs').existsSync('packages/typescript-estree/dist')) { process.exit(1); } console.log('DEPS_OK')"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
ROOT="$PWD"
"""

_BODY_SH = """build_all || true

rc=0
ran=0
run_pkg() {
    local pkg="$1"
    shift
    if [ ! -f "packages/$pkg/package.json" ]; then
        echo "MSWE_SKIP packages/$pkg (package not found)"
        return 0
    fi
    ran=1
    (cd "packages/$pkg" && MSWE_PKG="packages/$pkg" timeout --kill-after=60 3600 "$ROOT/node_modules/.bin/jest" "$@" --ci --coverage=false --maxWorkers=2 --passWithNoTests --reporters=default --reporters=/home/mswe_reporter.js < /dev/null) || rc=1
}

__RUNS__
[ "$ran" -eq 1 ] || { echo "MSWE: no test packages to run"; exit 1; }
exit "$rc"
"""

_BINARY_NO_DATA_RE = re.compile(r"^Binary files (?:a/\S+|/dev/null) and b/(\S+) differ$", re.M)


def _apply(pr: PullRequest) -> str:
    paths = sorted(
        set(_BINARY_NO_DATA_RE.findall(pr.test_patch or ""))
        | set(_BINARY_NO_DATA_RE.findall(pr.fix_patch or ""))
    )
    excludes = "".join(f" --exclude={shlex.quote(path)}" for path in paths)
    return f"git apply --whitespace=nowarn{excludes}"


def _is_test(rel: str) -> bool:
    if "/fixtures/" in rel or "/fixture-project/" in rel:
        return False
    return bool(
        _TEST_FILE_RE.search(rel)
        or _LIB_TEST_RE.match(rel)
        or rel == "tests/ast-alignment/spec.ts"
    )


def _plan(pr: PullRequest) -> list[tuple[str, list[str]]]:
    files: dict[str, set[str]] = {}
    touched: set[str] = set()
    for header in _DIFF_HEADER_RE.finditer(pr.test_patch or ""):
        match = _PKG_FILE_RE.match(header.group(2))
        if not match:
            continue
        pkg = match.group("pkg")
        rel = match.group("rel")
        if pkg == "shared-fixtures":
            touched.add("typescript-estree")
            continue
        if not rel.startswith("tests/"):
            continue
        if "/__snapshots__/" in rel and rel.endswith(".snap"):
            rel = rel.replace("/__snapshots__/", "/")[: -len(".snap")]
        if _is_test(rel):
            files.setdefault(pkg, set()).add(rel)
        else:
            touched.add(pkg)
    plan = [(pkg, sorted(rels)) for pkg, rels in files.items()]
    plan += [(pkg, []) for pkg in touched if pkg not in files]
    return sorted(plan)


def _runs(pr: PullRequest) -> str:
    lines = []
    for pkg, rels in _plan(pr):
        patterns = " ".join(shlex.quote(re.escape(rel) + "$") for rel in rels)
        lines.append(f"run_pkg {shlex.quote(pkg)} {patterns}".rstrip())
    return "\n".join(lines)


def _deps(pr: PullRequest) -> str:
    files = sorted(
        {
            header.group(2)
            for header in _DIFF_HEADER_RE.finditer(pr.fix_patch or "")
            if header.group(2) == "yarn.lock" or header.group(2).endswith("package.json")
        }
    )
    if "yarn.lock" not in files:
        return ""
    includes = " ".join(f"--include={shlex.quote(f)}" for f in files)
    return f"git apply --whitespace=nowarn {includes} /home/fix.patch"


class TypescriptEslint3199To6ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends --no-upgrade \\
    ca-certificates \\
    git \\
    python3 \\
    make \\
    g++ \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class TypescriptEslint3199To6ImageDefault(Image):
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
        return TypescriptEslint3199To6ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPORTER__", _REPORTER_JS)
            .replace("__ENV__", _ENV_SH)
            .replace("__DEPS__", _deps(self.pr))
            .replace("__RUNS__", _runs(self.pr))
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = self._script(_BODY_SH)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{_apply(self.pr)} /home/test.patch\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{_apply(self.pr)} /home/test.patch /home/fix.patch\n\n{body}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

    for line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n"):
        match = _RESULT_RE.search(line)
        if match:
            buckets[match.group(1)].add(match.group(2))

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


@Instance.register("typescript-eslint", _INTERVAL)
class TYPESCRIPT_ESLINT_3199_TO_6(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TypescriptEslint3199To6ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
