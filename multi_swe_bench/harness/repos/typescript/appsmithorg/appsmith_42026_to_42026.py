import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "appsmithorg"
_INTERVAL = "appsmith_42026_to_42026"
_BASE_IMAGE = "node:24.14.1-bookworm"
_CLIENT_DIR = "app/client"

_REPORTS_BEGIN = "===== MSB JEST RESULTS BEGIN ====="
_REPORTS_END = "===== MSB JEST RESULTS END ====="
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
export NODE_OPTIONS=--max-old-space-size=4096
export JEST_CACHE_DIR=/home/.msb-jest-cache
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export YARN_ENABLE_TELEMETRY=0
export CYPRESS_INSTALL_BINARY=0
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"""

_NET_RETRY = r"""net_retry() {
  local attempt=1 rc=0 log=/tmp/msb-net-retry.log
  while true; do
    rc=0
    "$@" > "$log" 2>&1 || rc=$?
    cat "$log"
    [ "$rc" -eq 0 ] && return 0
    if [ "$attempt" -ge 5 ] || ! grep -qiE 'ECONNRESET|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ENETUNREACH|EHOSTUNREACH|socket hang up|network error|RequestError|Could not resolve host|Connection reset|Connection timed out|Operation timed out|early EOF|unexpected disconnect|RPC failed|TLS connection|gnutls_handshake|SSL_ERROR|HTTP 5[0-9][0-9]|50[234] ' "$log"; then
      echo "msb: command failed with status $rc: $*" >&2
      return "$rc"
    fi
    echo "msb: network error, retrying ($attempt/5): $*" >&2
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
  done
}"""

_LIST_FILES = r"""grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u \
  | grep -E '^[[CLIENT]]/src/.+\.(test|spec)\.(tsx|ts|jsx|js)$' \
  | sed 's#^[[CLIENT]]/##' > /tmp/msb-jest-files.all || true"""

_DIGEST_JS = r"""const fs = require("fs");
const path = require("path");
const out = process.argv[2];
const prefix = process.argv[3];
const report = JSON.parse(fs.readFileSync(out, "utf8"));
for (const file of report.testResults || []) {
  const rel = prefix + "/" + path.relative(process.cwd(), file.name).split(path.sep).join("/");
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map((x) => String(x).replace(/\s+/g, " ").trim()).join(" > ");
    const status = t.status === "passed" ? "PASSED" : t.status === "failed" ? "FAILED" : "SKIPPED";
    console.log("MSB_RESULT " + status + " " + rel + "::" + title);
  }
  if (results.length === 0 && file.status === "failed") {
    console.log("MSB_SUITE_ERROR " + rel + ": " + String(file.message || "").replace(/\x1b\[[0-9;]*[a-zA-Z]/g, "").split("\n").filter((l) => l.trim()).slice(0, 3).join(" | "));
  }
}
"""

_RUN_TESTS = r"""cd /home/[[REPO]]/[[CLIENT]]
[[ENV]]
[[LIST_FILES]]
: > /tmp/msb-jest-files.txt
while read -r f; do
  if [ -f "$f" ]; then echo "$f" >> /tmp/msb-jest-files.txt; fi
done < /tmp/msb-jest-files.all
echo "MSB jest files:"
cat /tmp/msb-jest-files.txt
out=/tmp/msb-jest-report.json
rm -f "$out"
rc=0
if [ -s /tmp/msb-jest-files.txt ]; then
  node_modules/.bin/jest --ci --json --outputFile="$out" --maxWorkers=2 --cacheDirectory="$JEST_CACHE_DIR" --runTestsByPath $(cat /tmp/msb-jest-files.txt) < /dev/null || rc=$?
fi
echo "[[BEGIN]]"
if [ -f "$out" ]; then
  node - "$out" "[[CLIENT]]" <<'MSBJS'
[[DIGEST]]
MSBJS
fi
echo "[[END]]"
if [ -s /tmp/msb-jest-files.txt ] && [ ! -f "$out" ]; then
  echo "msb: jest exited with status $rc and wrote no report" >&2
  exit 1
fi
exit 0
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail

[[ENV]]

[[NET_RETRY]]

cd /home/[[REPO]]
git config --global --add safe.directory '*'
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e "[[SHA]]^{commit}" 2>/dev/null || net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

cd [[CLIENT]]
want_node="$(tr -d ' v\r\n' < .nvmrc)"
have_node="$(node --version | tr -d 'v')"
echo "msb: node $have_node, .nvmrc $want_node, engines $(node -p "require('./package.json').engines.node")"
[ "${have_node%%.*}" = "${want_node%%.*}" ] || { echo "msb: node major $have_node does not match .nvmrc $want_node" >&2; exit 1; }

yarn_version="$(node -p "require('./package.json').packageManager.split('@')[1].split('+')[0]")"
corepack enable
net_retry corepack prepare "yarn@$yarn_version" --activate
[ "$(yarn --version)" = "$yarn_version" ] || { echo "msb: yarn $yarn_version is not active" >&2; exit 1; }

YARN_ENABLE_SCRIPTS=false net_retry yarn install --immutable
[ -x node_modules/.bin/jest ] || { echo "msb: jest binary missing after install" >&2; exit 1; }
if [ -d node_modules/canvas ]; then
  net_retry yarn rebuild canvas
  node -e "require('canvas')" || { echo "msb: canvas native module failed to load" >&2; exit 1; }
fi

[[LIST_FILES]]
: > /tmp/msb-warmup.txt
for d in $(xargs -r -n1 dirname < /tmp/msb-jest-files.all | sort -u); do
  if [ -d "$d" ]; then
    find "$d" -maxdepth 1 -type f -regextype posix-extended -regex '.+\.(test|spec)\.(tsx|ts|jsx|js)$' >> /tmp/msb-warmup.txt
  fi
done
sort -u -o /tmp/msb-warmup.txt /tmp/msb-warmup.txt
if [ ! -s /tmp/msb-warmup.txt ]; then
  find src/utils -maxdepth 1 -type f -name '*.test.ts' | sort > /tmp/msb-warmup.all
  sed -n 1p /tmp/msb-warmup.all > /tmp/msb-warmup.txt
fi
echo "msb: jest warm-up files:"
cat /tmp/msb-warmup.txt
mkdir -p "$JEST_CACHE_DIR"
rm -f /tmp/msb-warmup.json
node_modules/.bin/jest --ci --silent --json --outputFile=/tmp/msb-warmup.json --maxWorkers=2 --cacheDirectory="$JEST_CACHE_DIR" --runTestsByPath $(cat /tmp/msb-warmup.txt) < /dev/null || true
[ -f /tmp/msb-warmup.json ] || { echo "msb: jest warm-up produced no report" >&2; exit 1; }
node -e "const r=require('/tmp/msb-warmup.json'); console.log('msb: warm-up tests passed=' + r.numPassedTests + ' failed=' + r.numFailedTests + ' suites not run=' + r.numRuntimeErrorTestSuites); if (r.numRuntimeErrorTestSuites > 0 || r.numPassedTests === 0) process.exit(1)" || { echo "msb: jest warm-up could not run the baseline suites" >&2; exit 1; }
rm -f /tmp/msb-warmup.json /tmp/msb-warmup.txt /tmp/msb-warmup.all /tmp/msb-jest-files.all /tmp/msb-net-retry.log

cd /home/[[REPO]]
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[LIST_FILES]]", _LIST_FILES)
        .replace("[[DIGEST]]", _DIGEST_JS.rstrip("\n"))
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
        .replace("[[CLIENT]]", _CLIENT_DIR)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Appsmith42026To42026ImageBase(Image):
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


class Appsmith42026To42026ImageDefault(Image):
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
        return Appsmith42026To42026ImageBase(self.pr, self._config)

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
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", "#!/bin/bash\n\n" + _render(_RUN_TESTS, self.pr)),
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
class Appsmith42026To42026(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Appsmith42026To42026ImageDefault(self.pr, self._config)

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
        start = text.find(_REPORTS_BEGIN)
        end = text.rfind(_REPORTS_END)
        if start == -1 or end == -1 or end < start:
            section = ""
        else:
            section = text[start + len(_REPORTS_BEGIN) : end]

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
