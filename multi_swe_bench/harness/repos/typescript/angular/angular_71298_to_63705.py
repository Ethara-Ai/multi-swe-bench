import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "angular_71298_to_63705"
_BASE_TAG = "base-63705_to_71298"
_NODE_IMAGE = "node:24.21.0-bookworm"
_BAZELISK_VERSION = "1.25.0"
_APT_PACKAGES = [
    "git",
    "ca-certificates",
    "curl",
    "build-essential",
    "python3",
    "zip",
    "unzip",
]

_BAZEL_STARTUP = "--batch --host_jvm_args=-Xmx4g"
_BAZEL_UI = "--color=no --curses=no --noshow_progress"
_BAZEL_BUILD = f"{_BAZEL_UI} --keep_going --jobs=4"
_BAZEL_TEST = (
    "--build_tests_only --nocache_test_results --test_output=errors "
    "--test_summary=short --test_timeout=1800 --local_test_jobs=2"
)

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_DELETED_FILE_RE = re.compile(r"^deleted file mode ", re.MULTILINE)
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


_BAZEL_RESULTS_JS = r"""'use strict';

const fs = require('fs');
const path = require('path');

const [targetsFile, testlogs, markerFile] = process.argv.slice(2);
const since = fs.statSync(markerFile).mtimeMs;

function emit(status, name) {
  process.stdout.write('MSB_RESULT ' + status + ' ' + name + '\n');
}

function decode(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#([0-9]+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

function attribute(attrs, key) {
  const match = new RegExp('(?:^|\\s)' + key + '="([^"]*)"').exec(attrs);
  return match ? decode(match[1]) : '';
}

function reports(dir) {
  const found = [];
  const direct = path.join(dir, 'test.xml');
  if (fs.existsSync(direct)) {
    found.push(direct);
  }
  let entries = [];
  try {
    entries = fs.readdirSync(dir);
  } catch (error) {
    entries = [];
  }
  for (const entry of entries.sort()) {
    if (/^shard_[0-9]+_of_[0-9]+$/.test(entry)) {
      const shard = path.join(dir, entry, 'test.xml');
      if (fs.existsSync(shard)) {
        found.push(shard);
      }
    }
  }
  return found.filter((file) => fs.statSync(file).mtimeMs >= since);
}

const labels = fs.readFileSync(targetsFile, 'utf8').split('\n').map((line) => line.trim()).filter(Boolean);

for (const label of labels) {
  const relative = label.replace(/^\/\//, '').replace(':', '/').replace(/^\//, '');
  const seen = new Map();
  let count = 0;
  for (const file of reports(path.join(testlogs, relative))) {
    const xml = fs.readFileSync(file, 'utf8');
    const testcase = /<testcase\b([^>]*?)(?:\/>|>([\s\S]*?)<\/testcase>)/g;
    let match;
    while ((match = testcase.exec(xml)) !== null) {
      const attrs = match[1];
      const body = match[2] || '';
      const failed = /<(failure|error)\b/.test(body);
      const skipped = /<skipped\b/.test(body) || /(?:^|\s)status="(notrun|skipped|disabled)"/.test(attrs);
      if (!failed && !skipped && attribute(attrs, 'time') === 'NaN') {
        continue;
      }
      const suite = attribute(attrs, 'classname');
      const title = attribute(attrs, 'name');
      let name = label + '::' + (suite ? suite + ' > ' + title : title === relative ? '<bazel target>' : title);
      const occurrence = (seen.get(name) || 0) + 1;
      seen.set(name, occurrence);
      if (occurrence > 1) {
        name += ' #' + occurrence;
      }
      let status = 'PASSED';
      if (failed) {
        status = 'FAILED';
      } else if (skipped) {
        status = 'SKIPPED';
      }
      emit(status, name);
      count += 1;
    }
  }
  if (count === 0) {
    emit('FAILED', label + '::<no test results>');
  }
}
"""


_PLAN_FN = r"""msb_bazel_log() {
    local log
    for log in "${HOME:-/root}"/.cache/bazel/_bazel_*/*/java.log; do
        if [ -f "$log" ]; then
            echo "=== MSB: bazel log $log ===" >&2
            tail -n 80 "$log" >&2
        fi
    done
}

msb_plan() {
    local out="$1"
    local path dir rc
    : > "$out/packages.txt"
    for path in "${declared[@]}"; do
        if [ ! -e "$path" ]; then
            continue
        fi
        dir=$(dirname "$path")
        while [ "$dir" != "." ] && [ ! -f "$dir/BUILD.bazel" ] && [ ! -f "$dir/BUILD" ]; do
            dir=$(dirname "$dir")
        done
        if [ "$dir" != "." ]; then
            echo "$dir" >> "$out/packages.txt"
        fi
    done
    sort -u -o "$out/packages.txt" "$out/packages.txt"
    : > "$out/targets.txt"
    if [ ! -s "$out/packages.txt" ]; then
        return 0
    fi
    {
        printf 'let t = tests(set('
        sed 's#^#//#; s#$#:all#' "$out/packages.txt" | tr '\n' ' '
        printf ')) in $t except attr("tags", "manual|no-sandbox", $t)\n'
    } > "$out/query.txt"
    rc=0
    timeout --kill-after=60 1800 bazel __BAZEL_STARTUP__ query __BAZEL_UI__ --keep_going --output=label --query_file="$out/query.txt" > "$out/targets.txt" < /dev/null || rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then
        msb_bazel_log
        echo "=== MSB: bazel query failed with status $rc ===" >&2
        return "$rc"
    fi
    sort -u -o "$out/targets.txt" "$out/targets.txt"
}"""


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

__ENV__

node --version
pnpm_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
npm install -g "pnpm@${pnpm_version}"
test "$(pnpm --version)" = "${pnpm_version}"

installed=0
for attempt in 1 2 3; do
    pnpm install --frozen-lockfile || true
    if [ -f node_modules/.modules.yaml ] && [ -e node_modules/jasmine-reporters ] && [ -x node_modules/.bin/tsc ]; then
        installed=1
        break
    fi
    echo "prepare: pnpm install attempt ${attempt} did not complete; retrying in 30s" >&2
    sleep 30
done
test "$installed" -eq 1

bazel_version="$(tr -d '[:space:]' < .bazelversion)"
test "$(bazel --version)" = "bazel ${bazel_version}"

declared=(__TEST_FILES__)

__PLAN_FN__

if [ "$(uname -m)" = "aarch64" ]; then
    warm=/tmp/msb-warm
    rm -rf "$warm"
    mkdir -p "$warm"
    if msb_plan "$warm" && [ -s "$warm/targets.txt" ]; then
        mapfile -t targets < "$warm/targets.txt"
        timeout --kill-after=120 3600 bazel __BAZEL_STARTUP__ test __BAZEL_BUILD__ __BAZEL_TEST__ -- "${targets[@]}" < /dev/null || true
    fi
    rm -rf "$warm" "${HOME:-/root}"/.cache/bazel/_bazel_root/cache/repos
fi

node -e "
const root = '/home/__REPO__';
['typescript', 'jasmine', 'jasmine-reporters'].forEach((id) => require(require.resolve(id, { paths: [root] })));
console.log('DEPS_OK');
"
"""


_ENV = r"""export CI=true
export HUSKY=0
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=8192
export CYPRESS_INSTALL_BINARY=0
export PUPPETEER_SKIP_DOWNLOAD=true
export npm_config_update_notifier=false"""


_TEST_BLOCK = r"""results=/tmp/msb-results
rm -rf "$results"
mkdir -p "$results"

declared=(__TEST_FILES__)
if [ "${#declared[@]}" -eq 0 ]; then
    echo "=== MSB: test.patch adds or changes no file ==="
    exit 1
fi

__PLAN_FN__

msb_plan "$results"

if [ ! -s "$results/targets.txt" ]; then
    echo "=== MSB: no node test target covers the test.patch files at this stage ==="
    exit 0
fi

echo "=== MSB: bazel test targets ==="
cat "$results/targets.txt"

testlogs="$(bazel __BAZEL_STARTUP__ info __BAZEL_UI__ bazel-testlogs < /dev/null)"
touch "$results/started"
mapfile -t targets < "$results/targets.txt"
rc=0
timeout --kill-after=120 14400 bazel __BAZEL_STARTUP__ test __BAZEL_BUILD__ __BAZEL_TEST__ -- "${targets[@]}" < /dev/null || rc=$?
echo "=== MSB: bazel test exited with status $rc ==="
node /home/msb_bazel_results.js "$results/targets.txt" "$testlogs" "$results/started"
exit "$rc"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

__ENV__

cd /home/__REPO__
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


def _test_files(pr: PullRequest) -> list[str]:
    files = set()
    for chunk in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(chunk)
        if not header or _DELETED_FILE_RE.search(chunk):
            continue
        files.add(header.group(2))
    return sorted(files)


class Angular71298To63705ImageBase(Image):
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

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {packages} \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64|arm64) ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://github.com/bazelbuild/bazelisk/releases/download/v{_BAZELISK_VERSION}/bazelisk-linux-${{arch}}" -o /usr/local/bin/bazel; \\
    chmod +x /usr/local/bin/bazel; \\
    ln -sf /usr/local/bin/bazel /usr/local/bin/bazelisk; \\
    node --version; \\
    git --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Angular71298To63705ImageDefault(Image):
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
        return Angular71298To63705ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        test_files = " ".join(shlex.quote(path) for path in _test_files(self.pr))
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__PLAN_FN__", _PLAN_FN)
            .replace("__ENV__", _ENV)
            .replace("__BAZEL_BUILD__", _BAZEL_BUILD)
            .replace("__BAZEL_TEST__", _BAZEL_TEST)
            .replace("__BAZEL_STARTUP__", _BAZEL_STARTUP)
            .replace("__BAZEL_UI__", _BAZEL_UI)
            .replace("__TEST_FILES__", test_files)
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
            File(".", "msb_bazel_results.js", _BAZEL_RESULTS_JS),
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


@Instance.register("angular", _INTERVAL)
@Instance.register("angular", "angular")
class ANGULAR_71298_TO_63705(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Angular71298To63705ImageDefault(self.pr, self._config)

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
