import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "node-red"
_INTERVAL = "node_red_6035_to_6035"
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
export NODE_ENV=test
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export NODE_OPTIONS=--max-old-space-size=4096
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
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ECONNREFUSED|EPIPE|socket hang up|network error|Could not resolve host|Connection reset|Connection timed out|RPC failed|early EOF|unexpected disconnect|the remote end hung up|TLS connection|gnutls_handshake|ERR_SOCKET_TIMEOUT|HTTP error 5[0-9][0-9]|status code 5[0-9][0-9]|50[0-9] (Bad Gateway|Service Unavailable|Gateway Time-?out|Internal Server Error)' "$log"; then
      echo "net_retry: '$*' failed with status $rc (attempt $attempt, not retried)" >&2
      return "$rc"
    fi
    echo "net_retry: network error in '$*', retrying (attempt $attempt)" >&2
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
  done
}
"""

_REPORTER_JS = r"""const fs = require('fs');

function MsbReporter(runner, options) {
  const opts = (options && options.reporterOptions) || {};
  const spec = opts.spec || 'unknown';
  const output = opts.output;
  const seen = new Map();
  const notes = [];
  const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
  const name = (t) => spec + '::' + t.titlePath().map(clean).join(' > ');
  const record = (t, status) => {
    const n = name(t);
    if (seen.get(n) === 'FAILED') return;
    seen.set(n, status);
  };
  const allTests = (s) => {
    let r = s.tests.slice();
    for (const c of s.suites) r = r.concat(allTests(c));
    return r;
  };
  runner.on('pass', (t) => record(t, 'PASSED'));
  runner.on('pending', (t) => record(t, 'SKIPPED'));
  runner.on('fail', (t, err) => {
    if (t.type === 'hook') {
      notes.push('MSB_HOOK_FAILED ' + spec + '::' + t.titlePath().map(clean).join(' > ') + ' :: ' + clean(err && err.message || err).slice(0, 300));
      if (t.ctx && t.ctx.currentTest && /"before each"/.test(t.title)) record(t.ctx.currentTest, 'FAILED');
      if (t.parent) {
        for (const x of allTests(t.parent)) {
          if (!seen.has(name(x))) record(x, 'FAILED');
        }
      }
    } else {
      record(t, 'FAILED');
    }
  });
  let written = false;
  const flush = () => {
    if (written || !output) return;
    written = true;
    const lines = notes.slice();
    for (const [n, s] of seen) lines.push('MSB_RESULT ' + s + ' ' + n);
    fs.writeFileSync(output, lines.join('\n') + '\n');
  };
  runner.once('end', flush);
  process.once('exit', flush);
}

module.exports = MsbReporter;
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
mkdir -p /tmp/msb
cat > /tmp/msb/reporter.js <<'MSBEOF'
[[REPORTER_JS]]
MSBEOF

DEPS=/home/msb-deps
sum=$(sha256sum package-lock.json | cut -c1-16)
active=$(cat "$DEPS/active" 2>/dev/null || true)
if [ "$sum" != "$active" ]; then
  if [ -d "$DEPS/$sum/node_modules" ]; then
    echo "=== MSB switching node_modules to prepared tree $sum ==="
    mkdir -p "$DEPS/$active"
    mv node_modules "$DEPS/$active/node_modules"
    mv "$DEPS/$sum/node_modules" node_modules
    echo "$sum" > "$DEPS/active"
  else
    echo "=== MSB no prepared node_modules for package-lock.json $sum, running npm ci ==="
    npm ci --prefer-offline --ignore-scripts < /dev/null || echo "=== MSB npm ci failed ==="
  fi
fi

grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | grep -E '(^|/)[^/]*_spec\.js$' | sort -u > /tmp/msb/specs.txt
echo "=== MSB specs ==="
cat /tmp/msb/specs.txt

rm -f /tmp/msb/result-*.txt
i=0
while read -r spec; do
  [ -n "$spec" ] || continue
  i=$((i + 1))
  if [ ! -f "$spec" ]; then
    echo "MSB_SUITE_ERROR $spec: file not found" > "/tmp/msb/result-$i.txt"
    continue
  fi
  echo "=== MSB spec $i: $spec ==="
  rc=0
  timeout -k 30 1200 ./node_modules/.bin/mocha --exit --reporter /tmp/msb/reporter.js --reporter-option "spec=$spec" --reporter-option "output=/tmp/msb/result-$i.txt" "$spec" < /dev/null || rc=$?
  echo "=== MSB spec $i exit code $rc ==="
  if [ ! -s "/tmp/msb/result-$i.txt" ]; then
    echo "MSB_SUITE_ERROR $spec: no results (exit code $rc)" > "/tmp/msb/result-$i.txt"
  fi
done < /tmp/msb/specs.txt

echo "[[BEGIN]]"
for f in $(ls /tmp/msb/result-*.txt 2>/dev/null | sort -V); do
  cat "$f"
done
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

node -e "const r=require('./package.json').engines||{};console.log('engines.node='+(r.node||'')+' node='+process.version)"

DEPS=/home/msb-deps
rm -rf "$DEPS" node_modules
mkdir -p "$DEPS"

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
fix_sum=$(sha256sum package-lock.json | cut -c1-16)
net_retry npm ci --ignore-scripts
mkdir -p "$DEPS/$fix_sum"
mv node_modules "$DEPS/$fix_sum/node_modules"
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh

base_sum=$(sha256sum package-lock.json | cut -c1-16)
if [ "$base_sum" = "$fix_sum" ]; then
  mv "$DEPS/$fix_sum/node_modules" node_modules
else
  net_retry npm ci --ignore-scripts
fi
echo "$base_sum" > "$DEPS/active"
npm cache clean --force > /dev/null 2>&1
bash /home/check_git_changes.sh

./node_modules/.bin/mocha --version
for d in "$DEPS"/*/node_modules; do
  [ -d "$d" ] || continue
  "$d/.bin/mocha" --version
done
git config --global user.email "msb@example.com"
git config --global user.name "msb"
git config --global init.defaultBranch main
bash /home/check_git_changes.sh

"""


def _strip_binary(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(
        s
        for s in sections
        if s and not re.search(r"^Binary files .* differ$", s, flags=re.MULTILINE)
    )


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[REPORTER_JS]]", _REPORTER_JS)
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class NodeRed6035To6035ImageBase(Image):
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


class NodeRed6035To6035ImageDefault(Image):
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
        return NodeRed6035To6035ImageBase(self.pr, self._config)

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
            File(".", "fix.patch", _strip_binary(self.pr.fix_patch)),
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
class NodeRed6035To6035(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NodeRed6035To6035ImageDefault(self.pr, self._config)

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
