import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "marko-js"
_INTERVAL = "marko_4469_to_4469"
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

_GREP_JS = r"""const fs = require('fs');
const files = fs.readFileSync(process.argv[2], 'utf8').split('\n').filter(Boolean);
const suites = { fixtures: 'runtime-tags/translator', 'fixtures-interop': 'translator-interop' };
const re = /^packages\/runtime-tags\/src\/__tests__\/(fixtures|fixtures-interop)\/([^/]+)\//;
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const groups = new Map();
for (const rel of files) {
  const m = re.exec(rel);
  if (!m || m[2].endsWith('.skip')) continue;
  if (!fs.existsSync('packages/runtime-tags/src/__tests__/' + m[1] + '/' + m[2] + '/template.marko')) continue;
  if (!groups.has(m[1])) groups.set(m[1], new Set());
  groups.get(m[1]).add(m[2]);
}
const parts = [];
for (const [dir, names] of groups) {
  parts.push('^' + esc(suites[dir]) + ' (' + [...names].sort().map(esc).join('|') + ') ');
}
console.log(parts.join('|'));
"""

_DIGEST_JS = r"""const fs = require('fs');
const out = process.argv[2];
const report = JSON.parse(fs.readFileSync(out, 'utf8'));
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
const seen = new Set();
const emit = (status, t) => {
  const name = clean(t.fullTitle);
  const key = status + ' ' + name;
  if (seen.has(key)) return;
  seen.add(key);
  console.log('MSB_RESULT ' + status + ' packages/runtime-tags/src/__tests__/main.test.ts::' + name);
};
for (const t of report.passes || []) emit('PASSED', t);
for (const t of report.failures || []) emit('FAILED', t);
for (const t of report.pending || []) emit('SKIPPED', t);
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
mkdir -p /tmp/msb
cat > /tmp/msb/grep.js <<'MSBEOF'
[[GREP_JS]]
MSBEOF
cat > /tmp/msb/digest.js <<'MSBEOF'
[[DIGEST_JS]]
MSBEOF

grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u > /tmp/msb/test-files.txt
pattern=$(node /tmp/msb/grep.js /tmp/msb/test-files.txt)
echo "=== MSB grep: $pattern ==="

out=/tmp/msb/mocha.json
rm -f "$out"
rc=0
if [ -n "$pattern" ]; then
  node_modules/.bin/mocha --config .mocharc.json --no-bail --timeout 120000 --reporter json --reporter-option "output=$out" --grep "$pattern" packages/runtime-tags/src/__tests__/main.test.ts < /dev/null || rc=$?
fi
echo "=== MSB mocha exit status: $rc ==="

echo "[[BEGIN]]"
if [ -f "$out" ]; then
  node /tmp/msb/digest.js "$out"
fi
echo "[[END]]"
if [ -n "$pattern" ] && [ ! -f "$out" ]; then
  echo "msb: mocha exited with status $rc and wrote no report" >&2
  exit 1
fi
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

echo "msb: node $(node --version), engines $(node -p "require('./package.json').engines.node")"
node -e "const v=process.versions.node.split('.').map(Number);const r=require('./package.json').engines.node;const ok=r.split('||').some(p=>{p=p.trim();const m=/^(\^|>=)?(\d+)\.?(\d+)?\.?(\d+)?$/.exec(p);if(!m)return false;const w=[+m[2],+(m[3]||0),+(m[4]||0)];const ge=v[0]>w[0]||(v[0]===w[0]&&(v[1]>w[1]||(v[1]===w[1]&&v[2]>=w[2])));return m[1]==='^'?ge&&v[0]===w[0]:ge;});if(!ok){console.error('msb: node '+process.version+' does not satisfy engines '+r);process.exit(1)}"

pm=$(node -p "require('./package.json').packageManager || ''")
[ -n "$pm" ] || { echo "prepare: package.json has no packageManager" >&2; exit 1; }
pm_name=${pm%%@*}
pm_version=${pm#*@}
pm_version=${pm_version%%+*}
[ "$pm_name" = "pnpm" ] || { echo "prepare: expected pnpm, got $pm_name" >&2; exit 1; }
corepack enable
net_retry corepack prepare "$pm_name@$pm_version" --activate
[ "$(pnpm --version)" = "$pm_version" ] || { echo "prepare: pnpm $pm_version is not active" >&2; exit 1; }

net_retry pnpm install --frozen-lockfile
bash /home/check_git_changes.sh

[ -x node_modules/.bin/mocha ] || { echo "prepare: mocha binary missing after install" >&2; exit 1; }
node_modules/.bin/mocha --version
warm=$(node -p "require('fs').readdirSync('packages/runtime-tags/src/__tests__/fixtures').sort().find((n) => !n.endsWith('.skip'))")
node_modules/.bin/mocha --config .mocharc.json --no-bail --timeout 120000 --reporter dot --grep "^runtime-tags/translator $warm " packages/runtime-tags/src/__tests__/main.test.ts < /dev/null
git clean -fdX packages/runtime-tags/src/__tests__/fixtures > /dev/null
rm -rf /tmp/msb
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[GREP_JS]]", _GREP_JS)
        .replace("[[DIGEST_JS]]", _DIGEST_JS)
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Marko4469To4469ImageBase(Image):
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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class Marko4469To4469ImageDefault(Image):
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
        return Marko4469To4469ImageBase(self.pr, self._config)

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
class Marko4469To4469(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Marko4469To4469ImageDefault(self.pr, self._config)

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
