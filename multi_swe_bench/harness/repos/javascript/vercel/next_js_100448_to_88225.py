import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "next_js_100448_to_88225"
_BASE_TAG = "base-88225_to_100448"
_NODE_IMAGE = "node:22.23.4-bookworm"
_RUSTUP_VERSION = "1.28.2"
_APT_PACKAGES = [
    "git",
    "ca-certificates",
    "curl",
    "build-essential",
    "cmake",
    "pkg-config",
    "libssl-dev",
    "clang",
    "libclang-dev",
    "python3",
]

_NATIVE_SOURCE_RE = r"\.rs$|(^|/)Cargo\.(toml|lock)$|^\.cargo/|^rust-toolchain|^(turbopack/)?crates/[^/]+/js/src/"
_NATIVE_SOURCE_PATTERN = re.compile(_NATIVE_SOURCE_RE)
_DIFF_PATHS_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^MSB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_JEST_RESULTS_JS = r"""'use strict';

const fs = require('fs');
const path = require('path');

const [label, testFile, reportPath, root] = process.argv.slice(2);

function emit(status, name) {
  process.stdout.write('\nMSB_RESULT ' + status + ' ' + name + '\n');
}

function clean(value) {
  return String(value === undefined || value === null ? '' : value).replace(/\s+/g, ' ').trim();
}

let report = null;
try {
  report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
} catch (error) {
  report = null;
}

if (!report) {
  emit('FAILED', label + '/' + testFile + '::<no jest report>');
  process.exit(0);
}

for (const suite of report.testResults || []) {
  const relative = path.relative(root, suite.name).split(path.sep).join('/');
  const tests = suite.assertionResults || [];
  const seen = new Map();
  for (const test of tests) {
    const titles = (test.ancestorTitles || []).concat(test.title).map(clean).filter(Boolean);
    let name = label + '/' + relative + '::' + titles.join(' > ');
    const count = (seen.get(name) || 0) + 1;
    seen.set(name, count);
    if (count > 1) {
      name += ' #' + count;
    }
    const status = test.status === 'passed' ? 'PASSED' : test.status === 'failed' ? 'FAILED' : 'SKIPPED';
    emit(status, name);
  }
  if (tests.length === 0 && (suite.status === 'failed' || suite.testExecError)) {
    emit('FAILED', label + '/' + relative + '::<suite failed to run>');
  }
}
"""


_PREPARE_ENV = r"""export CI=true
export HUSKY=0
export NEXT_TELEMETRY_DISABLED=1
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1
export NODE_OPTIONS=--max-old-space-size=8192
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export RUST_BACKTRACE=1
export npm_config_update_notifier=false
export NEXT_SKIP_NATIVE_POSTINSTALL=__SKIP_NATIVE_POSTINSTALL__"""


_RUN_ENV = r"""export CI=true
export HUSKY=0
export HEADLESS=true
export TURBO_TELEMETRY_DISABLED=1
export DO_NOT_TRACK=1
export NODE_OPTIONS=--max-old-space-size=8192
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export RUST_BACKTRACE=1"""


_NATIVE_FROM_SOURCE = r"""rustup toolchain install
rustup show active-toolchain
cargo --version
(cd packages/next-swc && pnpm run build-native-release-with-assertions)
ls packages/next-swc/native/next-swc.*.node"""


_NATIVE_PREBUILT = r"""ls node_modules/@next/swc-linux-*-gnu/next-swc.*.node"""


_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch --no-tags origin "__SHA__"
fi
git checkout --detach __SHA__
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

__PREPARE_ENV__

pnpm_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
npm install -g "pnpm@${pnpm_version}"
test "$(pnpm --version)" = "${pnpm_version}"

installed=0
for attempt in 1 2 3; do
    pnpm install --frozen-lockfile || true
    if [ -f node_modules/.modules.yaml ] && [ -x node_modules/.bin/jest ] && [ -x node_modules/.bin/turbo ]; then
        installed=1
        break
    fi
    echo "prepare: pnpm install attempt ${attempt} did not complete; retrying in 30s" >&2
    sleep 30
done
test "$installed" -eq 1

__NATIVE_BINDINGS__

pnpm exec turbo run build --concurrency=4

for attempt in 1 2 3; do
    if pnpm exec playwright install --with-deps chromium; then
        break
    fi
    echo "prepare: playwright install attempt ${attempt} failed; retrying in 20s" >&2
    sleep 20
done

node -e "
const fs = require('fs');
const root = '/home/__REPO__';
const resolve = (id) => require.resolve(id, { paths: [root] });
['jest', 'react', 'react-dom', 'execa'].forEach(resolve);
const { chromium } = require(resolve('playwright'));
if (!fs.existsSync(chromium.executablePath())) throw new Error('chromium is not installed: ' + chromium.executablePath());
if (!fs.existsSync(root + '/scripts/run-jest.sh')) throw new Error('scripts/run-jest.sh is missing');
const swc = require(root + '/packages/next/dist/build/swc/index.js');
swc.loadBindings().then((bindings) => {
  if (bindings.isWasm) throw new Error('wasm bindings were loaded instead of native bindings');
  console.log('DEPS_OK');
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
"
"""


_TEST_BLOCK = r"""results=/tmp/msb-results
rm -rf "$results"
mkdir -p "$results"
status=0

{ git diff --name-only HEAD; git ls-files --others --exclude-standard; } > "$results/changed-files.txt"
if grep -qE '__NATIVE_SOURCE_RE__' "$results/changed-files.txt"; then
    echo "=== MSB: rebuilding next-swc native bindings from source ==="
    (cd packages/next-swc && pnpm run build-native-release-with-assertions) > "$results/native-build.log" 2>&1 || { tail -n 80 "$results/native-build.log"; echo "=== MSB: native build failed ==="; exit 1; }
fi

echo "=== MSB: building workspace packages ==="
pnpm exec turbo run build --concurrency=4 > "$results/build.log" 2>&1 || { tail -n 80 "$results/build.log"; echo "=== MSB: workspace build failed ==="; exit 1; }

awk '/^diff --git a\// { sub(/^b\//, "", $4); print $4 }' /home/test.patch | sort -u > "$results/test-patch-files.txt"
awk '/\.test\.[cm]?[jt]sx?$/ && !/^(turbopack|crates)\//' "$results/test-patch-files.txt" > "$results/jest-files.txt"
awk '/^(turbopack\/)?crates\//' "$results/test-patch-files.txt" > "$results/rust-files.txt"

: > "$results/cargo-plan.txt"
while read -r rust_file; do
    crate_dir=$(dirname "$rust_file")
    while [ "$crate_dir" != "." ] && [ ! -f "$crate_dir/Cargo.toml" ]; do
        crate_dir=$(dirname "$crate_dir")
    done
    [ -f "$crate_dir/Cargo.toml" ] || continue
    crate=$(awk -F'"' '/^name *=/ { print $2; exit }' "$crate_dir/Cargo.toml")
    [ -n "$crate" ] || continue
    relative=${rust_file#"$crate_dir"/}
    case "$relative" in
        tests/*)
            target=${relative#tests/}
            target=${target%%/*}
            echo "$crate test ${target%.rs}" >> "$results/cargo-plan.txt"
            ;;
        *)
            echo "$crate lib lib" >> "$results/cargo-plan.txt"
            ;;
    esac
done < "$results/rust-files.txt"
sort -u -o "$results/cargo-plan.txt" "$results/cargo-plan.txt"

if [ ! -s "$results/jest-files.txt" ] && [ ! -s "$results/cargo-plan.txt" ]; then
    echo "=== MSB: test.patch contains no runnable jest or cargo tests ==="
    exit 1
fi

run_index=0
while read -r test_file; do
    [ -f "$test_file" ] || continue
    case "$test_file" in
        test/e2e/*) modes="dev start" ;;
        test/development/*) modes="dev" ;;
        test/production/*) modes="start" ;;
        *) modes="unit" ;;
    esac
    for mode in $modes; do
        run_index=$((run_index + 1))
        report="$results/jest-$run_index.json"
        mode_flag=""
        [ "$mode" = "unit" ] || mode_flag="--mode=$mode"
        echo "=== MSB_JEST: $mode $test_file ==="
        rc=0
        NODE_ENV=test PATH="/home/__REPO__/node_modules/.bin:$PATH" timeout --kill-after=60 3600 \
            bash scripts/run-jest.sh $mode_flag --bundler=turbo --headless -- \
            --runTestsByPath "$test_file" --forceExit --json --outputFile="$report" < /dev/null || rc=$?
        node /home/msb_jest_results.js "$mode" "$test_file" "$report" /home/__REPO__
        [ "$rc" -eq 0 ] || status=1
    done
done < "$results/jest-files.txt"

while read -r crate kind target; do
    if [ "$kind" = "test" ]; then
        selector="--test $target"
    else
        selector="--lib"
    fi
    log="$results/cargo-$crate-$target.log"
    echo "=== MSB_CARGO: $crate $kind $target ==="
    rc=0
    timeout --kill-after=60 10800 cargo test --profile release-with-assertions -p "$crate" $selector --no-fail-fast < /dev/null > "$log" 2>&1 || rc=$?
    tail -n 60 "$log"
    sed -nE \
        -e "s#^test (.+) \.\.\. ok\$#MSB_RESULT PASSED cargo/$crate/$target::\1#p" \
        -e "s#^test (.+) \.\.\. FAILED\$#MSB_RESULT FAILED cargo/$crate/$target::\1#p" \
        -e "s#^test (.+) \.\.\. ignored.*\$#MSB_RESULT SKIPPED cargo/$crate/$target::\1#p" \
        "$log"
    grep -qE '^test .+ \.\.\. (ok|FAILED|ignored)' "$log" || echo "MSB_RESULT FAILED cargo/$crate/$target::<no test results>"
    [ "$rc" -eq 0 ] || status=1
done < "$results/cargo-plan.txt"

exit "$status"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__RUN_ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


def _builds_native_from_source(pr: PullRequest) -> bool:
    for patch in (pr.fix_patch, pr.test_patch):
        for paths in _DIFF_PATHS_RE.findall(patch or ""):
            if any(_NATIVE_SOURCE_PATTERN.search(path) for path in paths):
                return True
    return False


class NextJs100448To88225ImageBase(Image):
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
        packages = " \\\n        ".join(_APT_PACKAGES)
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    PATH=/usr/local/cargo/bin:${{PATH}} \\
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {packages} \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) triple=x86_64-unknown-linux-gnu ;; \\
        arm64) triple=aarch64-unknown-linux-gnu ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl --proto '=https' --tlsv1.2 -fsSL "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/${{triple}}/rustup-init" -o /tmp/rustup-init; \\
    chmod +x /tmp/rustup-init; \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \\
    rm -f /tmp/rustup-init; \\
    rustup --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class NextJs100448To88225ImageDefault(Image):
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
        return NextJs100448To88225ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        from_source = _builds_native_from_source(self.pr)
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__PREPARE_ENV__", _PREPARE_ENV)
            .replace("__RUN_ENV__", _RUN_ENV)
            .replace(
                "__NATIVE_BINDINGS__",
                _NATIVE_FROM_SOURCE if from_source else _NATIVE_PREBUILT,
            )
            .replace("__SKIP_NATIVE_POSTINSTALL__", "1" if from_source else "0")
            .replace("__NATIVE_SOURCE_RE__", _NATIVE_SOURCE_RE)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
            File(".", "msb_jest_results.js", _JEST_RESULTS_JS),
        ]

    def dockerfile(self) -> str:
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        hardening = (
            Image._HARDENING_BLOCK.replace(
                "git gc --prune=now --aggressive",
                "git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now",
            )
            .replace(
                "git repack -a -d -l --quiet",
                "git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet",
            )
            .replace("${BASE_COMMIT}", self.pr.base.sha)
            .strip("\n")
        )
        return f"""FROM {self.dependency().image_full_name()}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}
"""


@Instance.register("vercel", _INTERVAL)
class NEXT_JS_100448_TO_88225(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NextJs100448To88225ImageDefault(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in clean_log.split("\n"):
            match = _RESULT_RE.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
