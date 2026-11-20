from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "typescript_eslint_11974_to_7065"
_BASE_IMAGE = "node:20.19.0-bookworm"
_BASE_TAG = "base-11974_to_7065"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_PKG_FILE_RE = re.compile(r"^packages/(?P<pkg>[^/]+)/(?P<rel>.+)$")
_TEST_FILE_RE = re.compile(r"^tests/.+\.(?:test|spec)\.[cm]?tsx?$")
_RESULT_RE = re.compile(r"@@TSE@@\t(PASS|FAIL|SKIP)\t(.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RULE_DIR_RE = re.compile(r"^tests/rules/[^/]+/")
_SKIP_PACKAGES = {"integration-tests", "website", "website-eslint"}
_DOCS_TEST = "tests/docs.test"

_JEST_REPORTER_JS = r"""'use strict';
const path = require('path');
class MsweJestReporter {
  emit(status, name) {
    process.stdout.write('\n@@TSE@@\t' + status + '\t' + name + '\n');
  }
  onTestResult(test, result) {
    const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
    const rel = path.relative(process.cwd(), result.testFilePath).split(path.sep).join('/');
    const file = (process.env.MSWE_PKG || '.') + '/' + rel;
    const items = result.testResults || [];
    if (result.testExecError || (result.failureMessage && items.length === 0)) {
      this.emit('FAIL', file + ' > (suite)');
    }
    for (const a of items) {
      const name = [file].concat(a.ancestorTitles || [], [a.title]).map(clean).join(' > ');
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
module.exports = MsweJestReporter;
"""

_VITEST_REPORTER_MJS = r"""import path from 'node:path';
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
const emit = (status, name) => process.stdout.write('\n@@TSE@@\t' + status + '\t' + name + '\n');
export default class MsweVitestReporter {
  constructor() {
    this.seen = new Set();
  }
  walk(file, task, parents) {
    if (task.type === 'suite') {
      for (const child of task.tasks || []) {
        this.walk(file, child, parents.concat([task.name]));
      }
      return;
    }
    const name = [file].concat(parents, [task.name]).map(clean).join(' > ');
    const state = task.result && task.result.state;
    if (task.mode === 'skip' || task.mode === 'todo' || state === 'skip' || state === 'todo') {
      emit('SKIP', name);
    } else if (state === 'pass') {
      emit('PASS', name);
    } else {
      emit('FAIL', name);
    }
  }
  report(files) {
    for (const f of files || []) {
      if (!f || this.seen.has(f.filepath)) {
        continue;
      }
      this.seen.add(f.filepath);
      const rel = path.relative(process.cwd(), f.filepath).split(path.sep).join('/');
      const file = (process.env.MSWE_PKG || '.') + '/' + rel;
      const errors = (f.result && f.result.errors) || [];
      if ((f.result && f.result.state === 'fail' && errors.length) || !(f.tasks || []).length) {
        if (f.result && f.result.state === 'fail') {
          emit('FAIL', file + ' > (suite)');
        }
      }
      for (const t of f.tasks || []) {
        this.walk(file, t, []);
      }
    }
  }
  onTestModuleEnd(testModule) {
    if (testModule && testModule.task) {
      this.report([testModule.task]);
    }
  }
  onTestRunEnd(testModules) {
    this.report((testModules || []).map((m) => m.task));
  }
  onFinished(files) {
    this.report(files);
  }
}
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
export SKIP_POSTINSTALL=1
export SKIP_POSTINSTALL_BUILD=1
export NX_DAEMON=false
export NX_NO_CLOUD=true
export NX_SKIP_NX_CACHE=true
export NX_TUI=false
export NX_SKIP_LOG_GROUPING=true
export YARN_ENABLE_GLOBAL_CACHE=true
export YARN_ENABLE_TELEMETRY=0
export YARN_ENABLE_PROGRESS_BARS=false
export NODE_OPTIONS=--max-old-space-size=6144
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0

yarn() {
    local yarn_path
    yarn_path="$(sed -n 's/^yarnPath: *//p' /home/__REPO__/.yarnrc.yml 2>/dev/null | tr -d "'\\"")"
    if [ -n "$yarn_path" ]; then
        node "/home/__REPO__/$yarn_path" "$@"
    else
        command yarn "$@"
    fi
}

pm_install() {
    if [ -f pnpm-lock.yaml ]; then
        corepack enable pnpm
        pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile
    elif [ -f .yarnrc.yml ]; then
        yarn install --immutable || YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install
    else
        yarn install --frozen-lockfile || yarn install
    fi
}

fix_swc() {
    [ "$(uname -m)" = aarch64 ] || return 0
    [ -f node_modules/@swc/core/package.json ] || return 0
    node -e "require('@swc/core').transformSync('1', {})" > /dev/null 2>&1 && return 0
    local version arch pkg tmp
    version="$(node -p "require('./node_modules/@swc/core/package.json').version")"
    case "$(uname -m)" in
        aarch64|arm64) arch=arm64 ;;
        *) arch=x64 ;;
    esac
    pkg="@swc/core-linux-${arch}-gnu"
    tmp="$(mktemp -d)"
    (cd "$tmp" && npm pack --silent "${pkg}@${version}" > /dev/null) || { rm -rf "$tmp"; return 1; }
    rm -rf "node_modules/${pkg}"
    mkdir -p "node_modules/${pkg}"
    tar -xzf "$tmp"/*.tgz -C "node_modules/${pkg}" --strip-components=1
    rm -rf "$tmp"
    node -e "require('@swc/core').transformSync('1', {})" > /dev/null
}

pm_build() {
    if [ -f pnpm-lock.yaml ]; then
        pnpm run build && return 0
    else
        yarn build && return 0
    fi
    local tsc_bin="$PWD/node_modules/.bin/tsc"
    for d in packages/*/; do
        [ -f "${d}tsconfig.build.json" ] || continue
        (cd "$d" && "$tsc_bin" -p tsconfig.build.json < /dev/null) || true
    done
    return 1
}

pm_clean() {
    if [ -f pnpm-lock.yaml ]; then
        pnpm store prune
    elif [ -f .yarnrc.yml ]; then
        yarn cache clean --all
    else
        yarn cache clean
    fi
}
"""

_PREPARE_SH = """#!/bin/bash
set -e

write_reporters() {
    cat > /home/mswe_jest_reporter.js <<'MSWE_JEST_REPORTER_EOF'
__JEST_REPORTER__MSWE_JEST_REPORTER_EOF
    cat > /home/mswe_vitest_reporter.mjs <<'MSWE_VITEST_REPORTER_EOF'
__VITEST_REPORTER__MSWE_VITEST_REPORTER_EOF
}

write_reporters

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__ENV__
__DEPS__
pm_install || true
fix_swc || true
git checkout -- .
git clean -fd
pm_build < /dev/null || true
pm_clean || true

node -e "require.resolve('typescript'); const fs = require('fs'); if (!fs.existsSync('node_modules/jest') && !fs.existsSync('node_modules/vitest')) { process.exit(1); } if (!fs.existsSync('packages/typescript-estree/dist')) { process.exit(1); } if (process.arch === 'arm64' && fs.existsSync('node_modules/@swc/core')) { require('@swc/core').transformSync('1', {}); } console.log('DEPS_OK')"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
ROOT="$PWD"
"""

_BODY_SH = """pm_build < /dev/null || true

VITEST_WORKERS="--maxWorkers=2"
if [ -f node_modules/vitest/package.json ] && [ "$(node -p "parseInt(require('./node_modules/vitest/package.json').version, 10)")" -lt 4 ]; then
    VITEST_WORKERS="--minWorkers=1 --maxWorkers=2"
fi

rc=0
ran=0
run_pkg() {
    local pkg="$1"
    local jest_args="$2"
    local vitest_args="$3"
    local dir="packages/$pkg"
    if [ ! -f "$dir/package.json" ]; then
        echo "MSWE_SKIP $dir (package not found)"
        return 0
    fi
    ran=1
    if ls "$dir"/vitest.config.* > /dev/null 2>&1; then
        eval "set -- $vitest_args"
        (cd "$dir" && MSWE_PKG="$dir" NODE_OPTIONS= timeout --kill-after=60 3600 "$ROOT/node_modules/.bin/vitest" run "$@" --passWithNoTests --coverage.enabled=false $VITEST_WORKERS --reporter=default --reporter=/home/mswe_vitest_reporter.mjs < /dev/null) || rc=1
    else
        eval "set -- $jest_args"
        (cd "$dir" && MSWE_PKG="$dir" NODE_OPTIONS="--experimental-vm-modules" timeout --kill-after=60 3600 "$ROOT/node_modules/.bin/jest" "$@" --ci --coverage=false --maxWorkers=2 --passWithNoTests --reporters=default --reporters=/home/mswe_jest_reporter.js < /dev/null) || rc=1
    fi
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


def _apply_with_fallback(pr: PullRequest, patches: str) -> str:
    apply = _apply(pr)
    steps = "\n".join(f"    {apply} {p} || {{ git add -A && {apply} --3way {p}; }}" for p in patches.split())
    return f"if ! {apply} {patches}; then\n    git checkout -- .\n    git clean -fdq\n{steps}\nfi"


def _helper_target(pkg: str, rel: str) -> str | None:
    if pkg == "eslint-plugin" and rel.startswith("tests/schema-snapshots/"):
        return "tests/schemas.test.ts"
    if pkg == "eslint-plugin" and rel.startswith("tests/docs-eslint-output-snapshots/"):
        return _DOCS_TEST
    if pkg == "scope-manager" and rel.startswith("tests/fixtures/"):
        return "tests/fixtures.test.ts"
    if pkg == "ast-spec" and (rel.endswith(".shot") or "/fixtures/" in rel):
        return "tests/fixtures.test.ts"
    return None


def _plan(pr: PullRequest) -> list[tuple[str, list[str]]]:
    files: dict[str, set[str]] = {}
    extras: dict[str, set[str]] = {}
    unmapped: set[str] = set()
    for header in _DIFF_HEADER_RE.finditer(pr.test_patch or ""):
        match = _PKG_FILE_RE.match(header.group(2))
        if not match:
            continue
        pkg = match.group("pkg")
        rel = match.group("rel")
        if pkg in _SKIP_PACKAGES:
            continue
        target = _helper_target(pkg, rel)
        if target:
            extras.setdefault(pkg, set()).add(target)
            continue
        if not rel.startswith("tests/"):
            continue
        if "/__snapshots__/" in rel and rel.endswith(".snap"):
            rel = rel.replace("/__snapshots__/", "/")[: -len(".snap")]
        if "/fixtures/" not in rel and _TEST_FILE_RE.match(rel):
            files.setdefault(pkg, set()).add(rel)
            continue
        rule_dir = _RULE_DIR_RE.match(rel)
        if rule_dir:
            extras.setdefault(pkg, set()).add(rule_dir.group(0))
        else:
            unmapped.add(pkg)
    plan: dict[str, set[str]] = {}
    for pkg in set(files) | set(extras) | unmapped:
        if pkg in files:
            plan[pkg] = files[pkg] | extras.get(pkg, set())
        elif pkg in unmapped:
            plan[pkg] = set()
        else:
            plan[pkg] = extras[pkg]
    return sorted((pkg, sorted(rels)) for pkg, rels in plan.items())


def _jest_pattern(rel: str) -> str:
    if rel == _DOCS_TEST:
        return re.escape(rel) + r"\.m?ts$"
    if rel.endswith("/"):
        return re.escape(rel)
    return re.escape(rel) + "$"


def _runs(pr: PullRequest) -> str:
    lines = []
    for pkg, rels in _plan(pr):
        jest_args = " ".join(shlex.quote(_jest_pattern(rel)) for rel in rels)
        vitest_args = " ".join(shlex.quote(rel) for rel in rels)
        lines.append(f"run_pkg {shlex.quote(pkg)} {shlex.quote(jest_args)} {shlex.quote(vitest_args)}")
    return "\n".join(lines)


def _dep_files(patch: str) -> list[str]:
    return sorted(
        {
            header.group(2)
            for header in _DIFF_HEADER_RE.finditer(patch or "")
            if header.group(2) == "yarn.lock"
            or header.group(2).endswith("package.json")
            or header.group(2).startswith(".yarn/patches/")
        }
    )


def _deps(pr: PullRequest) -> str:
    patches = [("/home/test.patch", _dep_files(pr.test_patch)), ("/home/fix.patch", _dep_files(pr.fix_patch))]
    if not any("yarn.lock" in files for _, files in patches):
        return ""
    lines = []
    for path, files in patches:
        if files:
            includes = " ".join(f"--include={shlex.quote(f)}" for f in files)
            lines.append(f"git apply --whitespace=nowarn {includes} {path}")
    return "\n".join(lines)


class TypescriptEslint11974To7065ImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
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


class TypescriptEslint11974To7065ImageDefault(Image):
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
        return TypescriptEslint11974To7065ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__JEST_REPORTER__", _JEST_REPORTER_JS)
            .replace("__VITEST_REPORTER__", _VITEST_REPORTER_MJS)
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
            File(".", "test-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch')}\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch /home/fix.patch')}\n\n{body}"),
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
class TYPESCRIPT_ESLINT_11974_TO_7065(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TypescriptEslint11974To7065ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
