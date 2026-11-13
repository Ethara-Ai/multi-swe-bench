import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "usebruno"
_REPO = "bruno"
_INTERVAL = "bruno_9425_to_8838"
_BASE_TAG = "base-8838_to_9425"
_NODE_IMAGE = "node:22.17.0-bookworm"
_REPORTER_NAME = "mswb_jest_reporter.cjs"
_REPORTER_PATH = f"/home/{_REPORTER_NAME}"
_RESULTS_PATH = "/tmp/mswb-jest.txt"

_JEST_SCRIPTS = {
    "bruno-js": "test:ci",
    "bruno-cli": "test:ci",
    "bruno-query": "test",
    "bruno-lang": "test",
    "bruno-schema": "test",
    "bruno-app": "test",
    "bruno-common": "test",
    "bruno-converters": "test:ci",
    "bruno-electron": "test:ci",
    "bruno-requests": "test",
    "bruno-filestore": "test",
}

_LIBRARY_BUILDS = (
    ("packages/bruno-graphql-docs", "npm run build:graphql-docs"),
    ("packages/bruno-query", "npm run build:bruno-query"),
    ("packages/bruno-common", "npm run build:bruno-common"),
    (
        "packages/bruno-js/src/sandbox/bundle-libraries.js",
        "npm run sandbox:bundle-libraries --workspace=packages/bruno-js",
    ),
    ("packages/bruno-converters", "npm run build:bruno-converters"),
    ("packages/bruno-requests", "npm run build:bruno-requests"),
    ("packages/bruno-schema-types", "npm run build:schema-types"),
    ("packages/bruno-filestore", "npm run build:bruno-filestore"),
    ("packages/bruno-sqlite", "npm run build:bruno-sqlite"),
)

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.MULTILINE)
_DELETED_FILE_RE = re.compile(r"^deleted file mode ", re.MULTILINE)
_UNIT_TEST_RE = re.compile(r"^packages/(bruno-[a-z-]+)/.+\.(?:spec|test)\.[jt]sx?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (\S.*?)\s*$")


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


_REPORTER_CJS = r"""'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = '/home/__REPO__';

const STATUS = {
  passed: 'PASSED',
  failed: 'FAILED',
  pending: 'SKIPPED',
  skipped: 'SKIPPED',
  todo: 'SKIPPED',
  disabled: 'SKIPPED',
};

function clean(value) {
  return String(value === undefined || value === null ? '' : value).replace(/\s+/g, ' ').trim();
}

class MswbJestReporter {
  constructor() {
    this.out = process.env.MSWB_RESULTS || '';
    this.seen = new Map();
  }

  emit(status, parts) {
    const base = parts.filter((part) => part.length > 0).join(' > ');
    const count = (this.seen.get(base) || 0) + 1;
    this.seen.set(base, count);
    const line = 'MSWB_RESULT ' + status + ' ' + (count > 1 ? base + ' #' + count : base) + '\n';
    if (this.out) {
      fs.appendFileSync(this.out, line);
    } else {
      process.stdout.write(line);
    }
  }

  onTestResult(test, testResult) {
    const file = path.relative(ROOT, testResult.testFilePath).split(path.sep).join('/');
    if (testResult.testExecError) {
      this.emit('FAILED', [file, '<file failed to run>']);
    }
    for (const result of testResult.testResults) {
      const status = STATUS[result.status];
      if (!status) {
        continue;
      }
      this.emit(status, [file].concat(result.ancestorTitles.map(clean), [clean(result.title)]));
    }
  }
}

module.exports = MswbJestReporter;
"""


_PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export HUSKY=0
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
export SCARF_ANALYTICS=false
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

node --version
npm --version

installed=0
for attempt in 1 2 3; do
    npm ci --legacy-peer-deps || true
    if [ -f node_modules/.package-lock.json ] && [ -x node_modules/.bin/jest ] && [ -x node_modules/.bin/rollup ] && [ -f node_modules/electron/path.txt ]; then
        installed=1
        break
    fi
    echo "prepare: npm ci attempt ${attempt} did not complete; retrying in 30s" >&2
    sleep 30
done
test "$installed" -eq 1

__LIBRARY_BUILDS__

test -s packages/bruno-js/src/sandbox/bundle-browser-rollup.js
test -s __REPORTER_PATH__
node node_modules/jest/bin/jest.js --version
node -e '
const assert = require("assert");
const { createRequire } = require("module");
const cliRequire = createRequire("/home/__REPO__/packages/bruno-cli/package.json");
const jsRequire = createRequire("/home/__REPO__/packages/bruno-js/package.json");
for (const id of ["@usebruno/common", "@usebruno/requests", "@usebruno/query", "@usebruno/converters", "@usebruno/filestore"]) {
  assert.ok(cliRequire(id), id);
}
for (const id of ["@usebruno/js", "@jest/globals"]) {
  console.log(id + " -> " + cliRequire.resolve(id));
}
console.log("quickjs-emscripten -> " + jsRequire.resolve("quickjs-emscripten"));
assert.equal(typeof cliRequire("electron"), "string");
assert.equal(typeof require("__REPORTER_PATH__"), "function");
'
echo DEPS_OK
"""


_STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export HUSKY=0
export TZ=UTC
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_update_notifier=false

cd /home/__REPO__
__APPLY__
results=__RESULTS_PATH__
rm -f "$results"
touch "$results"
export MSWB_RESULTS="$results"

declared=(__DECLARED__)
if [ "${#declared[@]}" -eq 0 ]; then
    echo "=== MSWB: test.patch adds or changes no jest unit test file under packages/ ==="
    exit 1
fi

present=()
for f in "${declared[@]}"; do
    if [ -f "$f" ]; then
        present+=("$f")
    fi
done

if [ "${#present[@]}" -eq 0 ]; then
    echo "=== MSWB: none of the test.patch unit test files exists at this stage ==="
    exit 0
fi

if [ -n "$(git status --porcelain -- __LIBRARY_PATHS__)" ]; then
__LIBRARY_BUILDS__
fi

rc=0
__JEST_RUNS__

for f in "${present[@]}"; do
    if ! grep -qF " $f > " "$results"; then
        echo "MSWB_RESULT FAILED $f > <no jest result>" >> "$results"
    fi
done

cat "$results"
exit "$rc"
"""

_TEST_APPLY = "git apply --whitespace=nowarn /home/test.patch\n"
_FIX_APPLY = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


def _test_files(pr: PullRequest) -> dict[str, list[str]]:
    files: dict[str, set[str]] = {}
    for chunk in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(chunk)
        if not header or _DELETED_FILE_RE.search(chunk):
            continue
        path = header.group(2)
        match = _UNIT_TEST_RE.match(path)
        if match and match.group(1) in _JEST_SCRIPTS:
            files.setdefault(match.group(1), set()).add(path)
    return {package: sorted(paths) for package, paths in sorted(files.items())}


def _jest_runs(tests: dict[str, list[str]]) -> str:
    blocks = []
    for package, paths in tests.items():
        exists = " || ".join(f"[ -f {shlex.quote(path)} ]" for path in paths)
        patterns = " ".join(
            shlex.quote(f"--testPathPattern={re.escape(path)}$") for path in paths
        )
        blocks.append(
            f"if {exists}; then\n"
            f"    timeout --kill-after=60 3600 npm run {_JEST_SCRIPTS[package]} "
            f"--workspace=packages/{package} -- {patterns} "
            f"--maxWorkers=2 --testTimeout=60000 --forceExit "
            f"--reporters=default --reporters={_REPORTER_PATH} < /dev/null || rc=$?\n"
            f"fi"
        )
    return "\n".join(blocks)


class Bruno9425To8838ImageBase(Image):
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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        python3 \\
        build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN node --version && npm --version && git --version

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Bruno9425To8838ImageDefault(Image):
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
        return Bruno9425To8838ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str, apply: str = "") -> str:
        tests = _test_files(self.pr)
        declared = " ".join(
            shlex.quote(path) for paths in tests.values() for path in paths
        )
        library_paths = " ".join(path for path, _ in _LIBRARY_BUILDS)
        library_builds = "\n".join(command for _, command in _LIBRARY_BUILDS)
        return (
            template.replace("__APPLY__", apply)
            .replace("__DECLARED__", declared)
            .replace("__LIBRARY_PATHS__", library_paths)
            .replace("__LIBRARY_BUILDS__", library_builds)
            .replace("__JEST_RUNS__", _jest_runs(tests))
            .replace("__RESULTS_PATH__", _RESULTS_PATH)
            .replace("__REPORTER_PATH__", _REPORTER_PATH)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", _REPORTER_NAME, self._render(_REPORTER_CJS)),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_STAGE_SH)),
            File(".", "test-run.sh", self._render(_STAGE_SH, _TEST_APPLY)),
            File(".", "fix-run.sh", self._render(_STAGE_SH, _FIX_APPLY)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return f"""FROM {image.image_full_name()}

{self.global_env}

ARG BASE_COMMIT="{sha}"

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _INTERVAL)
class BRUNO_9425_TO_8838(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Bruno9425To8838ImageDefault(self.pr, self._config)

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
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
