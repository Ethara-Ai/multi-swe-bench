import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "siddharthvaddem"
_INTERVAL = "openscreen_669_to_669"
_BASE_IMAGE = "node:22.22.1-bookworm"
_BASE_TAG = "base-node22"

_RESULTS_BEGIN = "===== MSB VITEST RESULTS BEGIN ====="
_RESULTS_END = "===== MSB VITEST RESULTS END ====="
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

_ENV = """export CI=true
export HUSKY=0
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_fund=false
export npm_config_audit=false"""

_NET_RETRY = r"""net_retry() {
  local attempt=1 rc=0 log=/tmp/msb-net-retry.log
  while true; do
    rc=0
    "$@" > "$log" 2>&1 || rc=$?
    cat "$log"
    [ "$rc" -eq 0 ] && return 0
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH|socket hang up|network error|Could not resolve host|Connection reset|Connection timed out|early EOF|unexpected disconnect|RPC failed|TLS connection|gnutls_handshake|HTTP 5[0-9][0-9]|50[234] ' "$log"; then
      echo "msb: command failed with status $rc: $*" >&2
      return "$rc"
    fi
    echo "msb: network error, retrying ($attempt/5): $*" >&2
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
  done
}"""

_LIST_FILES = r"""grep -E '^diff --git a/' /home/test.patch | awk '{print $4}' | sed 's#^b/##' | sort -u \
  | grep -E '^(src|electron)/.+\.(test|spec)\.(js|mjs|cjs|ts|mts|cts|jsx|tsx)$' \
  | grep -vE '\.browser\.test\.' > /tmp/msb-vitest-files.all || true"""

_DIGEST_JS = r"""const fs = require("fs");
const path = require("path");
const out = process.argv[2];
const root = process.argv[3];
let report;
try {
  report = JSON.parse(fs.readFileSync(out, "utf8"));
} catch (e) {
  console.log("MSB_SUITE_ERROR unreadable report");
  process.exit(0);
}
const clean = (s) => String(s).replace(/\s+/g, " ").trim();
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name).split(path.sep).join("/");
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map(clean).join(" > ");
    const status = t.status === "passed" ? "PASSED" : t.status === "failed" ? "FAILED" : "SKIPPED";
    console.log("MSB_RESULT " + status + " " + rel + "::" + title);
  }
  if (results.length === 0 && file.status === "failed") {
    console.log("MSB_SUITE_ERROR " + rel + ": " + clean(String(file.message || "").split("\n").slice(0, 3).join(" ")).slice(0, 300));
  }
}
"""

_RUN_TESTS = r"""cd /home/[[REPO]]
[[ENV]]
[[LIST_FILES]]
: > /tmp/msb-vitest-files.txt
while read -r f; do
  if [ -f "$f" ]; then echo "$f" >> /tmp/msb-vitest-files.txt; fi
done < /tmp/msb-vitest-files.all
echo "MSB vitest files:"
cat /tmp/msb-vitest-files.txt
out=/tmp/msb-vitest-report.json
rm -f "$out"
rc=0
if [ -s /tmp/msb-vitest-files.txt ]; then
  node_modules/.bin/vitest run --config vitest.config.ts --reporter=default --reporter=json --outputFile.json="$out" --testTimeout=60000 $(cat /tmp/msb-vitest-files.txt) < /dev/null || rc=$?
fi
echo "[[BEGIN]]"
if [ -f "$out" ]; then
  node - "$out" "/home/[[REPO]]" <<'MSBJS'
[[DIGEST]]
MSBJS
fi
echo "[[END]]"
if [ -s /tmp/msb-vitest-files.txt ] && [ ! -f "$out" ]; then
  echo "msb: vitest exited with status $rc and wrote no report" >&2
  exit 1
fi
exit 0
"""

_PREPARE = r"""#!/bin/bash
set -e

[[ENV]]

[[NET_RETRY]]

cd /home/[[REPO]]
git config --global --add safe.directory '*'
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e "[[SHA]]^{commit}" 2>/dev/null || net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

want_node="$(tr -d ' v\r\n' < .nvmrc)"
have_node="$(node --version | tr -d 'v')"
echo "msb: node $have_node, .nvmrc $want_node"
[ "${have_node%%.*}" = "${want_node%%.*}" ] || { echo "msb: node major $have_node does not match .nvmrc $want_node" >&2; exit 1; }

want_npm="$(node -p "((require('./package.json').packageManager || '').split('@')[1] || '').split('+')[0]")"
if [ -n "$want_npm" ] && [ "$(npm --version)" != "$want_npm" ]; then
  net_retry npm install -g "npm@$want_npm"
fi
echo "msb: npm $(npm --version)"

net_retry npm ci --ignore-scripts || true
npm rebuild esbuild || true
[ -x node_modules/.bin/vitest ] || { echo "msb: vitest binary missing after install" >&2; exit 1; }
npm ls --depth=0 > /dev/null
node_modules/.bin/vitest --version
rm -f /tmp/msb-net-retry.log

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[LIST_FILES]]", _LIST_FILES)
        .replace("[[DIGEST]]", _DIGEST_JS.rstrip("\n"))
        .replace("[[BEGIN]]", _RESULTS_BEGIN)
        .replace("[[END]]", _RESULTS_END)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


class Openscreen669To669ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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


class Openscreen669To669ImageDefault(Image):
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
        return Openscreen669To669ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        apply_and_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", "#!/bin/bash\nset -eo pipefail\n\n" + _render(_RUN_TESTS, self.pr)),
            File(
                ".",
                "test-run.sh",
                apply_and_run.format(repo=self.pr.repo, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                apply_and_run.format(
                    repo=self.pr.repo, patches="/home/test.patch /home/fix.patch"
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
class Openscreen669To669(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Openscreen669To669ImageDefault(self.pr, self._config)

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

        text = _ANSI_RE.sub("", test_log)
        start = text.find(_RESULTS_BEGIN)
        end = text.rfind(_RESULTS_END)
        if start == -1 or end == -1 or end < start:
            section = ""
        else:
            section = text[start + len(_RESULTS_BEGIN) : end]

        for line in section.splitlines():
            m = _RESULT_RE.match(line.strip())
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
