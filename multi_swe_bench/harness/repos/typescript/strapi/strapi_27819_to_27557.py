import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "strapi"
_INTERVAL = "strapi_27819_to_27557"
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
export NX_DAEMON=false
export NX_NO_CLOUD=true
export NX_SKIP_NX_CACHE=true
export NX_TUI=false
export NX_INTERACTIVE=false
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export CYPRESS_INSTALL_BINARY=0
export PUPPETEER_SKIP_DOWNLOAD=1
export NODE_OPTIONS=--max-old-space-size=6144
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
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ECONNREFUSED|EPIPE|socket hang up|network error|Could not resolve host|Connection reset|Connection timed out|RPC failed|early EOF|unexpected disconnect|the remote end hung up|TLS connection|gnutls_handshake|HTTP error 5[0-9][0-9]|status code 5[0-9][0-9]|Request failed "5[0-9][0-9]|503 Service|502 Bad Gateway|504 Gateway' "$log"; then
      echo "net_retry: '$*' failed with status $rc (attempt $attempt, not retried)" >&2
      return "$rc"
    fi
    echo "net_retry: network error in '$*', retrying (attempt $attempt)" >&2
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
  done
}
"""

_SELECT_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const files = fs.readFileSync(process.argv[3], 'utf8').split('\n').filter(Boolean);
const testRe = /(^|\/)__tests__\/.*\.(test|spec)\.[cm]?[jt]sx?$|\.(test|spec)\.[cm]?[jt]sx?$/;

const nearest = (rel, names) => {
  let dir = path.dirname(rel);
  while (dir && dir !== '.' && dir !== '/') {
    for (const n of names) {
      if (fs.existsSync(path.join(root, dir, n))) return { dir, cfg: n };
    }
    if (fs.existsSync(path.join(root, dir, 'package.json'))) return null;
    dir = path.dirname(dir);
  }
  return null;
};

const groups = new Map();
for (const rel of files) {
  if (!rel.startsWith('packages/')) continue;
  if (!testRe.test(rel)) continue;
  if (/\.test\.api\.|\.d\.ts$|\.test-d\./.test(rel)) continue;
  if (!fs.existsSync(path.join(root, rel))) continue;
  let runner;
  let hit;
  if (/\.vitest\.test\.[cm]?[jt]sx?$/.test(rel)) {
    runner = 'vitest';
    hit = nearest(rel, ['vitest.config.ts', 'vitest.config.mts', 'vitest.config.js', 'vitest.config.mjs']);
  } else {
    runner = 'jest';
    hit = nearest(rel, ['jest.config.js', 'jest.config.ts']);
    if (hit) {
      const sub = path.relative(hit.dir, rel);
      if (/^(ee\/)?admin\//.test(sub) && fs.existsSync(path.join(root, hit.dir, 'jest.config.front.js'))) {
        hit = { dir: hit.dir, cfg: 'jest.config.front.js' };
      }
    }
  }
  if (!hit) {
    console.error('MSB_SKIP_NO_CONFIG ' + rel);
    continue;
  }
  const key = runner + ' ' + hit.dir + ' ' + hit.cfg;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push(path.relative(hit.dir, rel));
}
for (const [key, list] of groups) {
  console.log(key + ' ' + list.join(' '));
}
"""

_CHANGED_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const files = fs.readFileSync(process.argv[3], 'utf8').split('\n').filter(Boolean);
const skipRe = /(^|\/)(__tests__|__mocks__)\/|\.(test|spec)\.[cm]?[jt]sx?$|\.test\.api\.|\.md$/;
const names = new Set();
for (const rel of files) {
  if (!rel.startsWith('packages/') || skipRe.test(rel)) continue;
  let dir = path.dirname(rel);
  while (dir && dir !== '.' && dir !== '/') {
    const pj = path.join(root, dir, 'package.json');
    if (fs.existsSync(pj)) {
      const pkg = JSON.parse(fs.readFileSync(pj, 'utf8'));
      if (pkg.name && pkg.scripts && pkg.scripts['build:code']) names.add(pkg.name);
      break;
    }
    dir = path.dirname(dir);
  }
}
console.log([...names].sort().join(','));
"""

_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const label = process.argv[3];
const out = process.argv[4];
if (!fs.existsSync(out)) {
  console.log('MSB_SUITE_ERROR ' + label + ': no report');
  process.exit(0);
}
let report;
try {
  report = JSON.parse(fs.readFileSync(out, 'utf8'));
} catch (e) {
  console.log('MSB_SUITE_ERROR ' + label + ': unreadable report');
  process.exit(0);
}
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map(clean).join(' > ');
    const status = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    console.log('MSB_RESULT ' + status + ' ' + rel + '::' + title);
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('MSB_SUITE_ERROR ' + rel + ': ' + clean(String(file.message || '').split('\n').slice(0, 3).join(' ')).slice(0, 300));
  }
}
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
mkdir -p /tmp/msb
cat > /tmp/msb/select.js <<'MSBEOF'
[[SELECT_JS]]
MSBEOF
cat > /tmp/msb/changed.js <<'MSBEOF'
[[CHANGED_JS]]
MSBEOF
cat > /tmp/msb/digest.js <<'MSBEOF'
[[DIGEST_JS]]
MSBEOF

ROOT=/home/[[REPO]]
grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u > /tmp/msb/test-files.txt
node /tmp/msb/select.js "$ROOT" /tmp/msb/test-files.txt > /tmp/msb/plan.txt
echo "=== MSB plan ==="
cat /tmp/msb/plan.txt

{ git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u > /tmp/msb/changed-files.txt
projects=$(node /tmp/msb/changed.js "$ROOT" /tmp/msb/changed-files.txt)
if [ -n "$projects" ]; then
  echo "=== MSB rebuilding: $projects ==="
  "$ROOT/node_modules/.bin/nx" run-many --target=build:code --projects="$projects" --nx-ignore-cycles --skip-nx-cache --outputStyle=static < /dev/null || echo "=== MSB rebuild of changed packages failed ==="
fi

rm -f /tmp/msb/report-*.json
i=0
while read -r runner dir cfg files; do
  [ -n "$runner" ] || continue
  i=$((i + 1))
  out="/tmp/msb/report-$i.json"
  echo "=== MSB suite $i: $runner $dir ($cfg) ==="
  if [ "$runner" = "vitest" ]; then
    (cd "$ROOT/$dir" && "$ROOT/node_modules/.bin/vitest" run --config "$cfg" --reporter=default --reporter=json --outputFile.json="$out" --testTimeout=60000 --passWithNoTests $files) < /dev/null || true
  else
    (cd "$ROOT/$dir" && "$ROOT/node_modules/.bin/jest" --config "$cfg" --ci --forceExit --testTimeout=60000 --passWithNoTests --json --outputFile="$out" --runTestsByPath $files) < /dev/null || true
  fi
done < /tmp/msb/plan.txt

echo "[[BEGIN]]"
i=0
while read -r runner dir cfg files; do
  [ -n "$runner" ] || continue
  i=$((i + 1))
  node /tmp/msb/digest.js "$ROOT" "$runner:$dir" "/tmp/msb/report-$i.json"
done < /tmp/msb/plan.txt
echo "[[END]]"
exit 0
"""

_PREPARE = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
[[ENV]]
[[NET_RETRY]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

pm=$(node -p "require('./package.json').packageManager || ''")
[ -n "$pm" ] || { echo "prepare: package.json has no packageManager" >&2; exit 1; }
pm_name=${pm%%@*}
pm_version=${pm#*@}
pm_version=${pm_version%%+*}
corepack enable
net_retry corepack prepare "$pm_name@$pm_version" --activate
[ "$(yarn --version)" = "$pm_version" ] || { echo "prepare: yarn $pm_version is not active" >&2; exit 1; }

net_retry yarn install --immutable
yarn cache clean --all > /dev/null 2>&1 || true
bash /home/check_git_changes.sh

./node_modules/.bin/nx run-many --target=build:code --nx-ignore-cycles --skip-nx-cache --outputStyle=static < /dev/null
bash /home/check_git_changes.sh

./node_modules/.bin/jest --version
./node_modules/.bin/vitest --version
./node_modules/.bin/nx --version > /dev/null
rm -rf /tmp/msb
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[SELECT_JS]]", _SELECT_JS)
        .replace("[[CHANGED_JS]]", _CHANGED_JS)
        .replace("[[DIGEST_JS]]", _DIGEST_JS)
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Strapi27819To27557ImageBase(Image):
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


class Strapi27819To27557ImageDefault(Image):
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
        return Strapi27819To27557ImageBase(self.pr, self._config)

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
class Strapi27819To27557(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Strapi27819To27557ImageDefault(self.pr, self._config)

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
