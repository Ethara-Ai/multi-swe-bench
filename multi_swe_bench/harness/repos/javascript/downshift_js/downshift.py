import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:16-bullseye"

BASE_TAG = "base"

JEST_BIN = "./node_modules/.bin/jest"

JEST_ARGS = (
    "--ci --colors=false --runInBand --verbose "
    "--json --outputFile=/home/jest-results.json"
)

NPM_FLAGS = (
    "--registry=https://registry.npmjs.org/ --legacy-peer-deps "
    "--no-audit --no-fund --no-progress "
    "--fetch-timeout=120000 --fetch-retries=5 --fetch-retry-maxtimeout=60000"
)

NPM_INSTALL_TIMEOUT = "5400"

JEST_BASELINE_TIMEOUT = "1200"

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
"""

EMIT_RESULTS_JS = r"""var fs = require('fs')
var path = require('path')

var ROOT = process.cwd()
var RESULTS = '/home/jest-results.json'
var BASELINE = '/home/baseline_tests.txt'

function rel(p) {
  if (!p) return ''
  return path.relative(ROOT, p).split(path.sep).join('/')
}

function idOf(a) {
  return (a.ancestorTitles || []).concat([a.title]).join(' > ')
}

function loadBaseline() {
  var m = {}
  if (!fs.existsSync(BASELINE)) return m
  fs.readFileSync(BASELINE, 'utf8').split('\n').forEach(function (line) {
    if (!line) return
    var i = line.indexOf('\t')
    if (i < 0) return
    var f = line.slice(0, i)
    var n = line.slice(i + 1)
    if (!m[f]) m[f] = []
    if (m[f].indexOf(n) < 0) m[f].push(n)
  })
  return m
}

var TITLE_RE = /^(\s*)(describe|it|test)(?:\.\w+)*\s*\(\s*(['"`])(.*?)\3\s*,/

function scanTitles(f) {
  var out = []
  var src
  try {
    src = fs.readFileSync(path.join(ROOT, f), 'utf8')
  } catch (e) {
    return out
  }
  var stack = []
  src.split('\n').forEach(function (line) {
    var m = line.match(TITLE_RE)
    if (!m) return
    var indent = m[1].length
    var kind = m[2]
    var title = m[4]
    while (stack.length && stack[stack.length - 1][0] >= indent) stack.pop()
    if (kind === 'describe') {
      stack.push([indent, title])
      return
    }
    var parts = stack.map(function (s) { return s[1] })
    parts.push(title)
    out.push(parts.join(' > '))
  })
  return out
}

function main() {
  var baseline = loadBaseline()
  var data = null
  try {
    data = JSON.parse(fs.readFileSync(RESULTS, 'utf8'))
  } catch (e) {
    data = null
  }

  var lines = []
  function emit(status, file, name) {
    lines.push('###TEST### ' + status + ' ' + file + '::' + name)
  }

  var seen = {}
  var suites = (data && data.testResults) || []

  suites.forEach(function (suite) {
    var f = rel(suite.name || suite.testFilePath)
    if (!f) return
    seen[f] = true
    var asserts = suite.assertionResults || []
    if (asserts.length === 0) {
      var names = (baseline[f] || []).slice()
      scanTitles(f).forEach(function (n) {
        if (names.indexOf(n) < 0) names.push(n)
      })
      names.forEach(function (n) { emit('FAIL', f, n) })
      return
    }
    asserts.forEach(function (a) {
      var s = a.status === 'passed' ? 'PASS' : a.status === 'failed' ? 'FAIL' : 'SKIP'
      emit(s, f, idOf(a))
    })
  })

  Object.keys(baseline).forEach(function (f) {
    if (seen[f]) return
    baseline[f].forEach(function (n) { emit('FAIL', f, n) })
  })

  process.stdout.write(lines.join('\n') + (lines.length ? '\n' : ''))
}

main()
"""

MAKE_BASELINE_JS = r"""var fs = require('fs')
var path = require('path')

var ROOT = process.cwd()
var data = JSON.parse(fs.readFileSync('/home/jest-results.json', 'utf8'))
var out = []
;(data.testResults || []).forEach(function (suite) {
  var f = path
    .relative(ROOT, suite.name || suite.testFilePath)
    .split(path.sep)
    .join('/')
  ;(suite.assertionResults || []).forEach(function (a) {
    out.push(f + '\t' + (a.ancestorTitles || []).concat([a.title]).join(' > '))
  })
})
fs.writeFileSync('/home/baseline_tests.txt', out.join('\n') + '\n')
process.stdout.write('baseline entries: ' + out.length + '\n')
"""


class DownshiftImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    CYPRESS_INSTALL_BINARY=0 \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    HUSKY=0 \\
    HUSKY_SKIP_INSTALL=1 \\
    npm_config_registry=https://registry.npmjs.org/ \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

{TOOLCHAIN_SETUP}
RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class DownshiftImageDefault(Image):
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
        return DownshiftImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "emit_results.js", EMIT_RESULTS_JS),
            File(".", "make_baseline.js", MAKE_BASELINE_JS),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

BEFORE_DATE=$(git show -s --format=%cI HEAD)
BEFORE_DAY=$(git show -s --format=%cs HEAD)
echo "prepare.sh: resolving dependencies published before $BEFORE_DATE"

timeout {npm_timeout} npm install --before="$BEFORE_DATE" {npm_flags} || true

if ! {jest_bin} --version > /dev/null 2>&1; then
  echo "prepare.sh: retrying with day-granularity --before=$BEFORE_DAY"
  rm -rf node_modules
  timeout {npm_timeout} npm install --before="$BEFORE_DAY" {npm_flags} || true
fi

if ! {jest_bin} --version > /dev/null 2>&1; then
  echo "prepare.sh: retrying without a --before constraint"
  rm -rf node_modules
  timeout {npm_timeout} npm install {npm_flags} || true
fi

if ! {jest_bin} --version > /dev/null 2>&1; then
  echo "prepare.sh: jest was not installed" >&2
  exit 1
fi

if [ "$(uname -m)" = "x86_64" ]; then
  rm -f /home/jest-results.json
  timeout {jest_timeout} env CI=true {jest_bin} {jest_args} > /dev/null 2>&1 || true
  if [ -f /home/jest-results.json ]; then
    node /home/make_baseline.js
  else
    echo "prepare.sh: no jest results produced for the baseline inventory" >&2
    exit 1
  fi
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: baseline inventory."
fi

rm -f package-lock.json
git checkout -- .
bash /home/check_git_changes.sh
""".format(
                    pr=self.pr,
                    npm_flags=NPM_FLAGS,
                    jest_bin=JEST_BIN,
                    jest_args=JEST_ARGS,
                    npm_timeout=NPM_INSTALL_TIMEOUT,
                    jest_timeout=JEST_BASELINE_TIMEOUT,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
rm -f /home/jest-results.json
set +e
{jest_bin} {jest_args} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
node /home/emit_results.js
exit $rc
""".format(pr=self.pr, jest_bin=JEST_BIN, jest_args=JEST_ARGS),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
rm -f /home/jest-results.json
set +e
{jest_bin} {jest_args} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
node /home/emit_results.js
exit $rc
""".format(pr=self.pr, jest_bin=JEST_BIN, jest_args=JEST_ARGS),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
rm -f /home/jest-results.json
set +e
{jest_bin} {jest_args} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
node /home/emit_results.js
exit $rc
""".format(pr=self.pr, jest_bin=JEST_BIN, jest_args=JEST_ARGS),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_EMIT = re.compile(r"^###TEST### (PASS|FAIL|SKIP) (.+)$")


@Instance.register("downshift-js", "downshift")
class Downshift(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DownshiftImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        def record(status: str, name: str) -> None:
            if status == "FAIL":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status == "PASS":
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            elif status == "SKIP":
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)

        for raw in _RE_ANSI.sub("", test_log).splitlines():
            match = _RE_EMIT.match(raw.strip())
            if match:
                record(match.group(1), match.group(2).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
