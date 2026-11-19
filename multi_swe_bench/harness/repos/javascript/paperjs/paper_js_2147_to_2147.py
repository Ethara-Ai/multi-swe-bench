import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# paperjs/paper.js, PR 2147 -- yarn 3 / QUnit era.
#
# Era (base 496050c): yarn.lock + .yarnrc.yml (yarnPath
# .yarn/releases/yarn-3.2.4.cjs), tests are QUnit files under test/tests/*.js
# run by gulp-qunits under Node. Node 24 is pinned by
# .github/workflows/ci.yml (`node-version: 24`). This config carries only the
# yarn 3 / QUnit toolchain; the later pnpm / Vitest era (PR 2470) has its own
# self-contained config, paper_js_2470_to_2470.py.
# Only the QUnit test files the test patch touches are run.

_INTERVAL = "paper_js_2147_to_2147"
# Official Docker library image, pulled through its ECR Public mirror (same
# multi-arch manifest as docker.io/library/node, without Docker Hub pull limits).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:24.21.0-bookworm"
# canvas (node-canvas 3.x) falls back to a source build when its prebuilt
# binary is unavailable; fonts-liberation gives Arial metrics the text tests
# expect (ci.yml "Install fonts").
_APT = (
    "git ca-certificates build-essential python3 pkg-config "
    "libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev "
    "fonts-liberation"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_QUNIT_FILE = re.compile(r"^test/tests/[^/]+\.js$")
# *.test.js files are not QUnit test files (they belong to the later Vitest era).
_NON_QUNIT_FILE = re.compile(r"\.test\.[cm]?js$")
_QUNIT_RESULT = re.compile(r"^QUNIT_RESULT (PASS|FAIL|SKIP) (.+)$")
_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
# Some base commits (e.g. PR 2147's, on the since-deleted fix/svg-clip-export
# branch) are not reachable from any ref the base image cloned; fetch exactly
# that commit by SHA before pinning to it.
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    git fetch --no-tags origin [[SHA]]
fi
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, pinned by this commit's lockfile) ----------
# Run the vendored yarn 3 release named by .yarnrc.yml yarnPath, as ci.yml
# does (`node .yarn/releases/yarn-3.2.4.cjs install --immutable`).
# The explicit --immutable flag (CI-faithful: the committed yarn.lock must
# resolve unchanged) overrides the base image's
# YARN_ENABLE_IMMUTABLE_INSTALLS=false default.
# Build scripts are skipped and only canvas' own install script is run:
# gulp-qunits' phantomjs-prebuilt postinstall has no linux/arm64 binary
# (and `yarn rebuild` re-runs it), and PhantomJS is only used by the
# browser runner, which these Node tests never start.
YARN_CJS=$(sed -n 's/^yarnPath: *//p' .yarnrc.yml)
node "$YARN_CJS" install --immutable --mode=skip-build
(cd node_modules/canvas && npm run install)

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Loads the native canvas binding and the QUnit runner the graded scripts use.
node -e "require('canvas'); require('jsdom'); require('qunitjs'); require('prepro/lib/node.js'); require('resemblejs')"
node -e "require('./dist/paper-full.js')"
echo DEPS_OK
"""

_QUNIT_RUNNER = r"""'use strict';
// Runs the selected paper.js QUnit test files under Node the way gulp-qunits'
// NodeRunner_Process does (same QUnit, Prepro.js and dist/paper-full.js, which
// is a tracked symlink to src/load.js), but prints one line per test instead of
// only the failed assertions.
// usage: node /home/paper_qunit.js <repo dir> <test/tests/X.js>...
var path = require('path');
var fs = require('fs');
var repo = path.resolve(process.argv[2]);
var files = process.argv.slice(3);
var testDir = path.join(repo, 'test');
var entry = path.join(testDir, 'msb-selected-load.js');
process.chdir(testDir);

var QUnit = require('qunitjs');
Object.keys(QUnit).forEach(function(k) { global[k] = QUnit[k]; });
global.QUnit = QUnit;

// Same preamble as test/load.js, then only the selected test files.
var body = "/*#*/ include('helpers.js');\n" +
    "/*#*/ include('../node_modules/resemblejs/resemble.js', { namespace: 'resemble' });\n";
files.forEach(function(f) {
    body += 'QUnit.config.msbFile = ' + JSON.stringify(f) + ';\n';
    body += '/*#*/ include(' + JSON.stringify(path.relative(testDir, path.join(repo, f))) + ');\n';
});
fs.writeFileSync(entry, body);

function cleanup() {
    try { fs.unlinkSync(entry); } catch (e) {}
}

var moduleFile = {};
var origModule = QUnit.module;
QUnit.module = global.module = function(name) {
    moduleFile[name] = QUnit.config.msbFile;
    return origModule.apply(this, arguments);
};

var messages = [];
QUnit.log(function(d) {
    if (!d.result) messages.push(d.message || '(no message)');
});
var seen = {};
QUnit.testDone(function(d) {
    var base = (moduleFile[d.module] || '?') + ' > ' + d.module + ' > ' + d.name;
    var name = base;
    for (var n = 2; seen[name]; n++) name = base + ' #' + n;
    seen[name] = true;
    var status = d.skipped ? 'SKIP' : (d.failed > 0 ? 'FAIL' : 'PASS');
    console.log('QUNIT_RESULT ' + status + ' ' + name);
    messages.forEach(function(m) { console.log('    ' + m); });
    messages = [];
});
QUnit.done(function(d) {
    console.log('QUNIT_DONE total=' + d.total + ' passed=' + d.passed + ' failed=' + d.failed);
    cleanup();
    process.exit(d.failed > 0 ? 1 : 0);
});
process.on('uncaughtException', function(err) {
    console.log('QUNIT_ERROR ' + ((err && err.stack) || err));
    cleanup();
    process.exit(1);
});

// gulp-qunits requires Prepro.js with addToGlobal, which exposes include():
Object.assign(global, require('prepro/lib/node.js'));
global.paper = require(path.join(repo, 'dist/paper-full.js'));
require(entry);
QUnit.load();
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
tests=()
while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && tests+=("$f")
done < /home/test_files.txt
echo "selected test files: ${tests[*]}"
NODE_PATH=/home/[[REPO]]/node_modules node /home/paper_qunit.js /home/[[REPO]] "${tests[@]}"
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in _FILE_HEADER.finditer(patch)))


def _test_files(pr: PullRequest) -> str:
    keep = [
        f
        for f in _patch_files(pr.test_patch)
        if _QUNIT_FILE.match(f)
        and not f.endswith("/load.js")
        and not _NON_QUNIT_FILE.search(f)
    ]
    return "".join(f"{f}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


class PaperJs2147To2147ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    YARN_ENABLE_TELEMETRY=0 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_ENABLE_GLOBAL_CACHE=false \\
    HUSKY=0 \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    node --version && npm --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PaperJs2147To2147ImageDefault(Image):
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
        return PaperJs2147To2147ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test_files.txt", _test_files(pr)),
            File(".", "paper_qunit.js", _QUNIT_RUNNER),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("paperjs", _INTERVAL)
class PAPER_JS_2147_TO_2147(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PaperJs2147To2147ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        for raw in clean.splitlines():
            line = raw.rstrip()
            match = _QUNIT_RESULT.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2).strip()
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
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
