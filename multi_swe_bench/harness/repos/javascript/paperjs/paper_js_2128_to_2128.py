import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "paperjs"
_INTERVAL = "paper_js_2128_to_2128"
_BASE_IMAGE = "node:24.22.1-bookworm"

_RESULTS_BEGIN = "===== MSB TEST RESULTS BEGIN ====="
_RESULTS_END = "===== MSB TEST RESULTS END ====="
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^MSB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_ENV = r"""export CI=true
export HUSKY=0
export TZ=UTC
export FORCE_COLOR=0
export NO_COLOR=1
export HUSKY_SKIP_INSTALL=1
export YARN_ENABLE_TELEMETRY=0
export YARN_ENABLE_GLOBAL_CACHE=false
export DEBIAN_FRONTEND=noninteractive
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
"""

_NET_RETRY = r"""net_retry() {
  local attempt=1
  local log=/tmp/msb-net-retry.log
  while true; do
    set +e
    "$@" 2>&1 | tee "$log"
    local rc=${PIPESTATUS[0]}
    set -e
    [ "$rc" -eq 0 ] && return 0
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ECONNREFUSED|ENETUNREACH|EHOSTUNREACH|EPIPE|socket hang up|network error|Could not resolve host|Connection reset|Connection timed out|RPC failed|early EOF|unexpected disconnect|the remote end hung up|TLS connection|gnutls_handshake|ERR_PNPM_META_FETCH_FAIL|ERR_PNPM_FETCH_|ERR_SOCKET_TIMEOUT|HTTP error 5[0-9][0-9]|status code 5[0-9][0-9]|50[234] ' "$log"; then
      echo "net_retry: '$*' failed with status $rc (attempt $attempt, not retried)" >&2
      return "$rc"
    fi
    echo "net_retry: network error in '$*', retrying (attempt $attempt)" >&2
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
  done
}
"""

_TEST_FILE = "gulp/typescript/typescript-definition-test.ts"

_DIGEST_JS = r"""const fs = require('fs');
const testFile = process.argv[2];
const genStatus = process.argv[3];
const tscLog = process.argv[4];
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
console.log('MSB_RESULT ' + (genStatus === '0' ? 'PASSED' : 'FAILED') + ' ' + testFile + '::docs:typescript:build generate dist/paper.d.ts');
if (genStatus !== '0' || !fs.existsSync(tscLog)) process.exit(0);
const errors = new Map();
const other = [];
const re = /^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$/;
for (const line of fs.readFileSync(tscLog, 'utf8').split('\n')) {
  const m = re.exec(line.trim());
  if (!m) {
    if (/error TS\d+/.test(line)) other.push(clean(line));
    continue;
  }
  if (m[1].replace(/\\/g, '/').endsWith(testFile.split('/').pop())) {
    const n = Number(m[2]);
    if (!errors.has(n)) errors.set(n, []);
    errors.get(n).push(m[4] + ' ' + clean(m[5]));
  } else {
    other.push(clean(line));
  }
}
const lines = fs.readFileSync(testFile, 'utf8').split('\n');
const counts = new Map();
const matched = new Set();
lines.forEach((raw, i) => {
  const text = clean(raw);
  if (!text || text.startsWith('//') || text.startsWith('/*') || text.startsWith('*')) return;
  if (!text.endsWith(';')) return;
  const n = (counts.get(text) || 0) + 1;
  counts.set(text, n);
  const name = testFile + '::' + text + (n > 1 ? ' #' + n : '');
  const errs = errors.get(i + 1);
  if (errs) {
    matched.add(i + 1);
    console.log('MSB_RESULT FAILED ' + name);
    for (const e of errs) console.log('MSB_DETAIL ' + (i + 1) + ': ' + e);
  } else {
    console.log('MSB_RESULT PASSED ' + name);
  }
});
for (const [n, errs] of errors) {
  if (!matched.has(n)) for (const e of errs) other.push(testFile + '(' + n + '): ' + e);
}
console.log('MSB_RESULT ' + (other.length ? 'FAILED' : 'PASSED') + ' ' + testFile + '::tsc --project gulp/typescript (definition and non-check errors)');
for (const e of other) console.log('MSB_DETAIL ' + e);
"""

_GENERATE = r"""gen_rc=0
version=$(node -p "require('./gulp/utils/options.js').version") || gen_rc=$?
date=$(node -p "require('./gulp/utils/options.js').date") || gen_rc=$?
rm -f gulp/typescript/typescript-definition-data.json
if [ "$gen_rc" -eq 0 ]; then
  (cd gulp/jsdoc && java -cp 'jsrun.jar:lib/*' JsRun app/run.js -c=conf/typescript.conf -D="file:../../gulp/typescript/typescript-definition-data.json" -D="version:$version" -D="date:$date") < /dev/null || gen_rc=$?
fi
if [ "$gen_rc" -eq 0 ] && [ ! -s gulp/typescript/typescript-definition-data.json ]; then
  echo "msb: jsdoc wrote no typescript definition data" >&2
  gen_rc=1
fi
if [ "$gen_rc" -eq 0 ]; then
  node gulp/typescript/typescript-definition-generator.js < /dev/null || gen_rc=$?
fi
tsc_rc=0
if [ "$gen_rc" -eq 0 ]; then
  node node_modules/typescript/bin/tsc --project gulp/typescript --noEmit --pretty false < /dev/null > /tmp/msb/tsc.log 2>&1 || tsc_rc=$?
  cat /tmp/msb/tsc.log
fi
echo "=== MSB generate status: $gen_rc, tsc status: $tsc_rc ==="
"""

_RESTORE = r"""rm -f gulp/typescript/typescript-definition-data.json gulp/typescript/typescript-definition-test.js
git checkout -- dist/paper.d.ts
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
rm -rf /tmp/msb
mkdir -p /tmp/msb
cat > /tmp/msb/digest.js <<'MSBEOF'
[[DIGEST_JS]]
MSBEOF

if ! grep -qE '^diff --git a/[[TEST_FILE]] ' /home/test.patch; then
  echo "msb: test.patch does not touch [[TEST_FILE]]" >&2
  exit 1
fi

[[GENERATE]]
echo "[[BEGIN]]"
node /tmp/msb/digest.js "[[TEST_FILE]]" "$gen_rc" /tmp/msb/tsc.log
echo "[[END]]"
[[RESTORE]]
exit 0
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
[[ENV]]
[[NET_RETRY]]
git config --global --add safe.directory '*'
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e "[[SHA]]^{commit}" 2>/dev/null || net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

net_retry git submodule update --init --recursive
bash /home/check_git_changes.sh

if ! command -v java > /dev/null 2>&1; then
  net_retry apt-get update
  net_retry apt-get install -y --no-install-recommends default-jre-headless
  rm -rf /var/lib/apt/lists/*
fi
java -version

echo "msb: node $(node --version), engines $(node -p "require('./package.json').engines.node")"
node -e "const v=process.versions.node.split('.').map(Number);const r=require('./package.json').engines.node;const m=/^>=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?$/.exec(r.trim());if(!m){console.error('msb: unsupported engines range '+r);process.exit(1)}const w=[+m[1],+(m[2]||0),+(m[3]||0)];const ok=v[0]>w[0]||(v[0]===w[0]&&(v[1]>w[1]||(v[1]===w[1]&&v[2]>=w[2])));if(!ok){console.error('msb: node '+process.version+' does not satisfy engines '+r);process.exit(1)}"

yarn_path=$(awk -F': *' '$1 == "yarnPath" {print $2}' .yarnrc.yml)
[ -n "$yarn_path" ] && [ -f "$yarn_path" ] || { echo "prepare: .yarnrc.yml has no usable yarnPath" >&2; exit 1; }
node "$yarn_path" --version
YARN_ENABLE_SCRIPTS=false net_retry node "$yarn_path" install --immutable
bash /home/check_git_changes.sh

[ -f node_modules/typescript/bin/tsc ] || { echo "prepare: typescript missing after install" >&2; exit 1; }
node node_modules/typescript/bin/tsc --version
node -e "require('mustache')"

mkdir -p /tmp/msb
[[GENERATE]]
[ "$gen_rc" -eq 0 ] || { echo "prepare: definition generation failed at base" >&2; exit 1; }
[[RESTORE]]
rm -rf /tmp/msb
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[GENERATE]]", _GENERATE)
        .replace("[[RESTORE]]", _RESTORE)
        .replace("[[DIGEST_JS]]", _DIGEST_JS)
        .replace("[[TEST_FILE]]", _TEST_FILE)
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class PaperJs2128To2128ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
            sha = self.pr.base.sha
            code = f'RUN git clone {url} /home/{self.pr.repo} && cd /home/{self.pr.repo} && {{ git cat-file -e "{sha}^{{commit}}" 2>/dev/null || git fetch --no-tags {url} {sha}; }}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class PaperJs2128To2128ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return PaperJs2128To2128ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        apply_and_run = """#!/bin/bash

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", "#!/bin/bash\n\n" + _render(_RUN_TESTS, self.pr)),
            File(
                ".",
                "test-run.sh",
                apply_and_run.format(repo=repo, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                apply_and_run.format(
                    repo=repo, patches="/home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register(_ORG, _INTERVAL)
class PaperJs2128To2128(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PaperJs2128To2128ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        start = test_log.find(_RESULTS_BEGIN)
        end = test_log.rfind(_RESULTS_END)
        if start == -1 or end == -1 or end < start:
            section = ""
        else:
            section = test_log[start + len(_RESULTS_BEGIN) : end]

        for raw in _ANSI_RE.sub("", section).splitlines():
            m = _RESULT_RE.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
