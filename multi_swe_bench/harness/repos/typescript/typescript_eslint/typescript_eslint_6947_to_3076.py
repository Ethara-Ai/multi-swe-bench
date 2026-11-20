from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "typescript_eslint_6947_to_3076"
_BASE_IMAGE = "node:18.20.8-bookworm"
_BASE_TAG = "base-6947_to_3076"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_PKG_FILE_RE = re.compile(r"^packages/(?P<pkg>[^/]+)/(?P<rel>.+)$")
_NEW_PKG_RE = re.compile(r"^diff --git a/\S+ b/packages/[^/]+/package\.json\nnew file mode", re.M)
_TEST_FILE_RE = re.compile(r"^tests/.+\.(?:test|spec)\.[cm]?tsx?$")
_LIB_TEST_RE = re.compile(r"^tests/lib/.+\.[cm]?[jt]sx?$")
_RULE_DIR_RE = re.compile(r"^tests/rules/[^/]+/")
_RESULT_RE = re.compile(r"@@TSE@@\t(PASS|FAIL|SKIP)\t(.+?)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SKIP_PACKAGES = {"integration-tests", "website", "website-eslint"}
_LIB_TEST_PACKAGES = {"parser", "typescript-estree"}
_ALIGNMENT_SPEC = "tests/ast-alignment/spec.ts"
_DOCS_TEST = "tests/docs.test"
_BUILD_ORDER = (
    "ast-spec types visitor-keys typescript-estree scope-manager experimental-utils utils "
    "type-utils parser rule-tester eslint-plugin eslint-plugin-internal eslint-plugin-tslint"
)

_REPORTER_JS = r"""'use strict';
const path = require('path');
class MsweReporter {
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
export SKIP_POSTINSTALL_BUILD=1
export NX_DAEMON=false
export NX_NO_CLOUD=true
export NX_SKIP_NX_CACHE=true
export NX_SKIP_LOG_GROUPING=true
export NX_TUI=false
export NODE_OPTIONS=--max-old-space-size=6144

link_workspaces() {
    for d in packages/*/; do
        n=$(node -p "require('./${d}package.json').name" 2>/dev/null) || continue
        [ -e "node_modules/$n" ] || { mkdir -p "$(dirname "node_modules/$n")"; ln -s "$PWD/$d" "node_modules/$n"; }
    done
}

build_all() {
    local tsc="$PWD/node_modules/.bin/tsc"
    rm -rf node_modules/.cache/nx .nx/cache
    yarn build --skip-nx-cache < /dev/null && return 0
    local root="$PWD"
    for p in __BUILD_ORDER__; do
        [ -f "packages/$p/tsconfig.build.json" ] || continue
        if [ "$p" = types ] && [ -f packages/types/tools/copy-ast-spec.ts ]; then
            (cd packages/types && yarn prebuild < /dev/null) || true
        fi
        (cd "packages/$p" && "$tsc" -p tsconfig.build.json < /dev/null) || true
        if [ "$p" = ast-spec ] && [ -f packages/ast-spec/api-extractor.json ]; then
            (cd packages/ast-spec && "$root/node_modules/.bin/api-extractor" run --local < /dev/null) || true
        fi
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
    || yarn install --ignore-engines --network-timeout 600000 \\
    || true
git checkout -- .
git clean -fd
if [ -d patches ] && [ -x node_modules/.bin/patch-package ]; then
    node_modules/.bin/patch-package || true
fi
if [ "$(uname -m)" = aarch64 ] && [ -f node_modules/@swc/core/package.json ] && ! node -e "require('@swc/core').transformSync('1', {})" > /dev/null 2>&1; then
    swc_version=$(node -p "require('@swc/core/package.json').version")
    npm install --no-save --no-package-lock --prefix /tmp/swc-arm64 "@swc/core-linux-arm64-gnu@${swc_version}"
    rm -rf node_modules/@swc/core-linux-arm64-gnu
    cp -r /tmp/swc-arm64/node_modules/@swc/core-linux-arm64-gnu node_modules/@swc/
    rm -rf /tmp/swc-arm64
    node -e "require('@swc/core').transformSync('1', {})"
fi
build_all || true
yarn cache clean || true

node -e "['jest', 'typescript'].forEach((m) => require.resolve(m)); if (!require('fs').existsSync('packages/typescript-estree/dist')) { process.exit(1); } if (process.arch === 'arm64' && require('fs').existsSync('node_modules/@swc/core')) { require('@swc/core').transformSync('1', {}); } console.log('DEPS_OK')"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
ROOT="$PWD"
"""

_BODY_SH = """link_workspaces
build_all || true

rc=0
ran=0
run_pkg() {
    local pkg="$1"
    shift
    local dir="packages/$pkg"
    if [ ! -f "$dir/package.json" ]; then
        echo "MSWE_SKIP $dir (package not found)"
        return 0
    fi
    ran=1
    (cd "$dir" && MSWE_PKG="$dir" NODE_OPTIONS="--experimental-vm-modules" timeout --kill-after=60 3600 "$ROOT/node_modules/.bin/jest" "$@" --ci --coverage=false --maxWorkers=2 --passWithNoTests --reporters=default --reporters=/home/mswe_reporter.js < /dev/null) || rc=1
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


def _is_test(pkg: str, rel: str) -> bool:
    if "/fixtures/" in rel or "/fixture-project/" in rel:
        return False
    if _TEST_FILE_RE.match(rel) or rel == _ALIGNMENT_SPEC:
        return True
    return pkg in _LIB_TEST_PACKAGES and bool(_LIB_TEST_RE.match(rel))


def _helper_target(pkg: str, rel: str) -> str | None:
    if pkg == "typescript-estree" and rel.startswith("tests/ast-alignment/"):
        return _ALIGNMENT_SPEC
    if pkg == "typescript-estree" and rel.startswith("tests/snapshots/"):
        return "tests/ast-fixtures.test.ts"
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
        if "/__snapshots__/" in rel and rel.endswith(".snap"):
            rel = rel.replace("/__snapshots__/", "/")[: -len(".snap")]
        if rel.startswith("tests/") and _is_test(pkg, rel):
            files.setdefault(pkg, set()).add(rel)
            continue
        target = _helper_target(pkg, rel)
        if target:
            extras.setdefault(pkg, set()).add(target)
            continue
        if not rel.startswith("tests/"):
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


def _pattern(rel: str) -> str:
    if rel == _DOCS_TEST:
        return re.escape(rel) + r"\.m?ts$"
    if rel.endswith("/"):
        return re.escape(rel)
    return re.escape(rel) + "$"


def _runs(pr: PullRequest) -> str:
    lines = []
    for pkg, rels in _plan(pr):
        patterns = " ".join(shlex.quote(_pattern(rel)) for rel in rels)
        lines.append(f"run_pkg {shlex.quote(pkg)} {patterns}".rstrip())
    return "\n".join(lines)


def _dep_files(patch: str) -> list[str]:
    return sorted(
        {
            header.group(2)
            for header in _DIFF_HEADER_RE.finditer(patch or "")
            if header.group(2) == "yarn.lock"
            or header.group(2).endswith("package.json")
            or header.group(2).startswith("patches/")
        }
    )


def _deps(pr: PullRequest) -> str:
    patches = [("/home/test.patch", _dep_files(pr.test_patch)), ("/home/fix.patch", _dep_files(pr.fix_patch))]
    new_workspace = any(_NEW_PKG_RE.search(patch or "") for patch in (pr.test_patch, pr.fix_patch))
    if not new_workspace and not any("yarn.lock" in files for _, files in patches):
        return ""
    lines = []
    for path, files in patches:
        if files:
            includes = " ".join(f"--include={shlex.quote(f)}" for f in files)
            lines.append(f"git apply --whitespace=nowarn {includes} {path}")
    return "\n".join(lines)


class TypescriptEslint6947To3076ImageBase(Image):
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


class TypescriptEslint6947To3076ImageDefault(Image):
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
        return TypescriptEslint6947To3076ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__REPORTER__", _REPORTER_JS)
            .replace("__ENV__", _ENV_SH)
            .replace("__BUILD_ORDER__", _BUILD_ORDER)
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
class TYPESCRIPT_ESLINT_6947_TO_3076(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TypescriptEslint6947To3076ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
