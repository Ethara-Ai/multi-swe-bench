import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "mozilla"
_INTERVAL = "pdf_js_22334_to_21474"
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
export TESTING=true
export PUPPETEER_SKIP_DOWNLOAD=1
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false
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
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ECONNREFUSED|EPIPE|socket hang up|network error|Could not resolve host|Connection reset|Connection timed out|RPC failed|early EOF|unexpected disconnect|the remote end hung up|TLS connection|gnutls_handshake|HTTP error 5[0-9][0-9]|status code 5[0-9][0-9]|code E5[0-9][0-9]|50[0-9] Service|502 Bad Gateway|504 Gateway' "$log"; then
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
const cfg = JSON.parse(fs.readFileSync(path.join(root, 'test/unit/clitests.json'), 'utf8'));
const listed = new Set(cfg.spec_files || []);
for (const rel of files) {
  const m = /^test\/unit\/([^/]+_spec\.js)$/.exec(rel);
  if (!m) {
    console.error('MSB_SKIP_NOT_UNIT ' + rel);
    continue;
  }
  if (!fs.existsSync(path.join(root, rel))) {
    console.error('MSB_SKIP_MISSING ' + rel);
    continue;
  }
  if (!listed.has(m[1])) {
    console.error('MSB_SKIP_NOT_CLI ' + rel);
    continue;
  }
  console.log(m[1]);
}
"""

_CONFIG_JS = r"""const fs = require('fs');
const path = require('path');

const root = process.argv[2];
const spec = process.argv[3];
const out = process.argv[4];
const cfg = JSON.parse(fs.readFileSync(path.join(root, 'test/unit/clitests.json'), 'utf8'));
cfg.helpers = [...(cfg.helpers || []), 'msb_reporter.mjs'];
cfg.spec_files = [spec];
fs.writeFileSync(out, JSON.stringify(cfg, null, 2));
console.log(cfg.spec_dir);
"""

_REPORTER_MJS = r"""import fs from "fs";

const out = process.env.MSB_RESULTS_FILE;
const spec = process.env.MSB_SPEC;
const stack = [];
const clean = s => String(s).replace(/\s+/g, " ").trim();
const write = line => fs.appendFileSync(out, line + "\n");

jasmine.getEnv().addReporter({
  suiteStarted(result) {
    stack.push(clean(result.description));
  },
  suiteDone(result) {
    stack.pop();
    if (result.status === "failed") {
      const msg = (result.failedExpectations || []).map(e => clean(e.message)).join(" | ");
      console.log("MSB_SUITE_ERROR " + spec + "::" + clean(result.fullName) + ": " + msg.slice(0, 300));
    }
  },
  specDone(result) {
    const status =
      result.status === "passed" ? "PASSED" : result.status === "failed" ? "FAILED" : "SKIPPED";
    write("MSB_RESULT " + status + " " + spec + "::" + [...stack, clean(result.description)].join(" > "));
  },
  jasmineDone(result) {
    for (const e of result.failedExpectations || []) {
      console.log("MSB_GLOBAL_ERROR " + spec + ": " + clean(e.message).slice(0, 300));
    }
  },
});
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
ROOT=/home/[[REPO]]
rm -rf /tmp/msb
mkdir -p /tmp/msb
cat > /tmp/msb/select.js <<'MSBEOF'
[[SELECT_JS]]
MSBEOF
cat > /tmp/msb/config.js <<'MSBEOF'
[[CONFIG_JS]]
MSBEOF
cat > /tmp/msb/msb_reporter.mjs <<'MSBEOF'
[[REPORTER_MJS]]
MSBEOF

grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u > /tmp/msb/test-files.txt
node /tmp/msb/select.js "$ROOT" /tmp/msb/test-files.txt > /tmp/msb/plan.txt
echo "=== MSB plan ==="
cat /tmp/msb/plan.txt

: > /tmp/msb/results.txt
if [ -s /tmp/msb/plan.txt ]; then
  echo "=== MSB building lib-legacy ==="
  if ! "$ROOT/node_modules/.bin/gulp" lib-legacy < /dev/null; then
    echo "=== MSB lib-legacy build failed, removing stale build ==="
    rm -rf "$ROOT/build/lib-legacy"
  fi
  while read -r spec; do
    [ -n "$spec" ] || continue
    spec_dir=$(node /tmp/msb/config.js "$ROOT" "$spec" /tmp/msb/jasmine.json)
    echo "=== MSB running test/unit/$spec ==="
    if [ ! -d "$ROOT/$spec_dir" ]; then
      echo "MSB_SUITE_ERROR test/unit/$spec: $spec_dir missing"
      continue
    fi
    cp /tmp/msb/msb_reporter.mjs "$ROOT/$spec_dir/msb_reporter.mjs"
    rc=0
    MSB_SPEC="test/unit/$spec" MSB_RESULTS_FILE=/tmp/msb/results.txt \
      node --enable-source-maps "$ROOT/node_modules/jasmine/bin/jasmine" JASMINE_CONFIG_PATH=/tmp/msb/jasmine.json < /dev/null || rc=$?
    echo "=== MSB jasmine exit $rc for test/unit/$spec ==="
    rm -f "$ROOT/$spec_dir/msb_reporter.mjs"
  done < /tmp/msb/plan.txt
fi

echo "[[BEGIN]]"
cat /tmp/msb/results.txt
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

[ -f package-lock.json ] || { echo "prepare: package-lock.json not found" >&2; exit 1; }
net_retry npm ci
bash /home/check_git_changes.sh

engines=$(node -p "(require('./package.json').engines || {}).node || ''")
if [ -n "$engines" ]; then
  node -e "process.exit(require('semver').satisfies(process.version, process.argv[1]) ? 0 : 1)" "$engines" || { echo "prepare: node $(node --version) does not satisfy engines $engines" >&2; exit 1; }
fi

./node_modules/.bin/gulp lib-legacy < /dev/null
test -d build/lib-legacy/test/unit
node node_modules/jasmine/bin/jasmine version
npm cache clean --force > /dev/null 2>&1
rm -rf /tmp/msb /tmp/msb-net-retry.log
bash /home/check_git_changes.sh

"""

_APPLY_AND_RUN = r"""#!/bin/bash

cd /home/[[REPO]]
excludes=()
for patch in [[PATCHES]]; do
  while read -r f; do
    [ -n "$f" ] && excludes+=("--exclude=$f")
  done < <(awk '/^diff --git /{f=$4; sub(/^b\//, "", f)} /^Binary files /{print f}' "$patch")
done
if [ "${#excludes[@]}" -gt 0 ]; then
  echo "Skipping binary diffs: ${excludes[*]}"
fi
if ! git apply --whitespace=nowarn "${excludes[@]}" [[PATCHES]]; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[SELECT_JS]]", _SELECT_JS)
        .replace("[[CONFIG_JS]]", _CONFIG_JS)
        .replace("[[REPORTER_MJS]]", _REPORTER_MJS)
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class PdfJs22334To21474ImageBase(Image):
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


class PdfJs22334To21474ImageDefault(Image):
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
        return PdfJs22334To21474ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", "#!/bin/bash\n\n" + _render(_RUN_TESTS, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(_APPLY_AND_RUN, self.pr).replace(
                    "[[PATCHES]]", "/home/test.patch"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(_APPLY_AND_RUN, self.pr).replace(
                    "[[PATCHES]]", "/home/test.patch /home/fix.patch"
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
class PdfJs22334To21474(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PdfJs22334To21474ImageDefault(self.pr, self._config)

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
