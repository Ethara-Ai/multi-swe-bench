import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "next_3534_to_551"
_NODE_IMAGE = "node:12.22.12-bullseye"
_DART_SASS_VERSION = "1.32.13"
_BINARY_RE = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TEST_RE = re.compile(r"KARMA_(TEST|RETEST)\|(PASS|FAIL|SKIP)\|(.+?)\s*$")


def _excludes(*patches: str) -> str:
    paths = {p for patch in patches for m in _BINARY_RE.finditer(patch or "") for p in m.groups() if p}
    return " ".join(shlex.quote(f"--exclude={path}") for path in sorted(paths))


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __NODE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \
    CI=true \
    CHROME_BIN=/usr/bin/chromium

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN sed -i '/bullseye-security/d' /etc/apt/sources.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    python3 \
    sudo \
    wget \
    chromium \
    fonts-liberation \
    xvfb \
    xauth \
    libgtk-3-0 \
    libgbm1 \
    libnotify4 \
    libnss3 \
    libxss1 \
    libasound2 \
    libxtst6 \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/dart-sass \
    && cd /opt/dart-sass \
    && npm install --no-audit --no-fund --no-package-lock --no-save sass@__DART_SASS_VERSION__ \
    && node -e "require('/opt/dart-sass/node_modules/sass')"

__GLOBAL_ENV__

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

ARG BASE_COMMIT="__SHA__"

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 1; \
    git config --local pack.windowMemory 32m; \
    git config --local pack.packSizeLimit 128m; \
    git config --local pack.deltaCacheSize 32m; \
    git gc --prune=now; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git config --local pack.threads 1; \
            git config --local pack.windowMemory 32m; \
            git config --local pack.packSizeLimit 128m; \
            git config --local pack.deltaCacheSize 32m; \
            git gc --prune=now; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi

__CLEAR_ENV__
"""

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"; exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"; git status --porcelain; exit 1
fi
echo "check_git_changes: No uncommitted changes"
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch --no-tags origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

cat > /home/karma.ci.js <<'NEXT_KARMA_EOF'
__KARMA_CI_JS__NEXT_KARMA_EOF

install_deps() {
    local mode="$1"
    node --version
    npm --version

    args=(--ignore-scripts --no-audit --no-fund --legacy-peer-deps --no-package-lock)
    if [ "${mode}" = "base" ]; then
        args+=(--before="$(git show -s --format=%cI HEAD)")
    fi

    for attempt in 1 2 3; do
        if npm install "${args[@]}"; then
            break
        fi
        if [ "${attempt}" -eq 3 ]; then
            echo "deps: npm install failed after ${attempt} attempts" >&2
            exit 1
        fi
        echo "deps: npm install attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done

    rm -rf node_modules/node-sass
    mkdir -p node_modules/node-sass
    printf '%s\n' '{"name":"node-sass","version":"4.14.1","main":"index.js"}' > node_modules/node-sass/package.json
    printf '%s\n' "module.exports = require('/opt/dart-sass/node_modules/sass');" > node_modules/node-sass/index.js

    test -x node_modules/.bin/karma
    test -f scripts/test/karma.js
    node -e "['karma','karma-mocha','karma-webpack','karma-chrome-launcher','mocha','webpack','node-sass'].forEach(function(m){require.resolve(m)}); require('node-sass').renderSync({data:'a{b:c}'}); console.log('DEPS_OK')"
}

install_deps base
"""

_KARMA_CI_JS = r"""var path = require('path');

var repo = '/home/__REPO__';
var baseConf = require(path.join(repo, 'scripts/test/karma.js'));
process.env.CHROME_BIN = '/usr/bin/chromium';

var spec = process.env.KARMA_SPEC;
var tag = process.env.CI_TEST_TAG || 'TEST';
var specAbs = path.resolve(repo, spec);

function clean(text) {
  return String(text).replace(/\s+/g, ' ').trim();
}

function CIReporter(baseReporterDecorator) {
  baseReporterDecorator(this);
  var self = this;
  var seen = {};
  self.onSpecComplete = function (browser, result) {
    var parts = (result.suite || []).concat([result.description]).map(clean);
    var name = spec + ' > ' + parts.join(' > ');
    seen[name] = (seen[name] || 0) + 1;
    if (seen[name] > 1) {
      name += ' #' + seen[name];
    }
    var status = result.skipped ? 'SKIP' : (result.success ? 'PASS' : 'FAIL');
    self.write('\nKARMA_' + tag + '|' + status + '|' + name + '\n');
  };
  self.onBrowserError = function (browser, error) {
    self.write('\nKARMA_BROWSER_ERROR|' + spec + '|' + clean(String(error).split('\n')[0]) + '\n');
  };
  self.onRunComplete = function (browsers, results) {
    self.write('\nKARMA_RUN_COMPLETE|' + spec + '|' + results.success + '|' + results.failed + '|' + results.error + '\n');
  };
}
CIReporter.$inject = ['baseReporterDecorator'];

module.exports = function (config) {
  config.runAll = true;
  config.a11y = false;
  config.v2 = false;
  baseConf(config);

  var files = config.files.slice(0, -1).concat([specAbs]);
  var preprocessors = {};
  preprocessors[specAbs] = ['webpack', 'sourcemap'];

  config.set({
    basePath: repo, files: files, preprocessors: preprocessors,
    singleRun: true, autoWatch: false, colors: false, concurrency: 1,
    captureTimeout: 120000, browserNoActivityTimeout: 120000,
    browserDisconnectTimeout: 30000, browserDisconnectTolerance: 0,
    customLaunchers: {
      ChromeHeadlessCI: {
        base: 'ChromeHeadless',
        flags: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
      }
    }
  });

  config.client = config.client || {};
  config.client.mocha = Object.assign({}, config.client.mocha, { retries: 2 });
  config.browsers = ['ChromeHeadlessCI'];
  config.reporters = ['ci'];
  config.plugins = (config.plugins || []).filter(function (plugin) {
    return plugin !== 'karma-coverage' && plugin !== 'karma-spec-reporter';
  });
  config.plugins.push({ 'reporter:ci': ['type', CIReporter] });
  config.logLevel = config.LOG_WARN;
};
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

set +e
set -u
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium

rc=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

mapfile -t karma_specs < <(find test -type f -name '*-spec.js' -not -path '*/node_modules/*' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
if [ "${#karma_specs[@]}" -eq 0 ]; then
    echo "KARMA_NO_SPECS"
    exit 1
fi
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

exit "${rc}"
"""

_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch

set +e
set -u
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium

rc=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

mapfile -t karma_specs < <(find test -type f -name '*-spec.js' -not -path '*/node_modules/*' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
if [ "${#karma_specs[@]}" -eq 0 ]; then
    echo "KARMA_NO_SPECS"
    exit 1
fi
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

exit "${rc}"
"""

_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
cd /home/__REPO__

git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch

install_deps() {
    local mode="$1"
    node --version
    npm --version

    args=(--ignore-scripts --no-audit --no-fund --legacy-peer-deps --no-package-lock)
    if [ "${mode}" = "base" ]; then
        args+=(--before="$(git show -s --format=%cI HEAD)")
    fi

    for attempt in 1 2 3; do
        if npm install "${args[@]}"; then
            break
        fi
        if [ "${attempt}" -eq 3 ]; then
            echo "deps: npm install failed after ${attempt} attempts" >&2
            exit 1
        fi
        echo "deps: npm install attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done

    rm -rf node_modules/node-sass
    mkdir -p node_modules/node-sass
    printf '%s\n' '{"name":"node-sass","version":"4.14.1","main":"index.js"}' > node_modules/node-sass/package.json
    printf '%s\n' "module.exports = require('/opt/dart-sass/node_modules/sass');" > node_modules/node-sass/index.js

    test -x node_modules/.bin/karma
    test -f scripts/test/karma.js
    node -e "['karma','karma-mocha','karma-webpack','karma-chrome-launcher','mocha','webpack','node-sass'].forEach(function(m){require.resolve(m)}); require('node-sass').renderSync({data:'a{b:c}'}); console.log('DEPS_OK')"
}

if ! git diff --quiet HEAD -- package.json package-lock.json; then
    install_deps fix
fi

set +e
set -u
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium

rc=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

mapfile -t karma_specs < <(find test -type f -name '*-spec.js' -not -path '*/node_modules/*' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
if [ "${#karma_specs[@]}" -eq 0 ]; then
    echo "KARMA_NO_SPECS"
    exit 1
fi
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

exit "${rc}"
"""


class _NextImage_3534_TO_551(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class NextImageBase_3534_TO_551(_NextImage_3534_TO_551):
    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-next_3534_to_551"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = 'RUN git clone "${REPO_URL}" /home/__REPO__'
        else:
            code = "COPY __REPO__ /home/__REPO__"
        return (
            _BASE_DOCKERFILE.replace("__NODE_IMAGE__", _NODE_IMAGE)
            .replace("__DART_SASS_VERSION__", _DART_SASS_VERSION)
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class NextImageDefault_3534_TO_551(_NextImage_3534_TO_551):
    def dependency(self) -> Image:
        return NextImageBase_3534_TO_551(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__EXCLUDES__", _excludes(self.pr.test_patch, self.pr.fix_patch))
        )

    def files(self) -> list[File]:
        prepare = self._render(_PREPARE_SH).replace("__KARMA_CI_JS__", self._render(_KARMA_CI_JS))
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency().image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
        )


@Instance.register("alibaba-fusion", _INTERVAL)
class NEXT_3534_TO_551(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NextImageDefault_3534_TO_551(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        first: dict[str, str] = {}
        retest: dict[str, str] = {}
        text = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in text.splitlines():
            match = _TEST_RE.search(line)
            if not match:
                continue
            kind, status, name = match.groups()
            bucket = first if kind == "TEST" else retest
            if bucket.get(name) == "FAIL" or (bucket.get(name) == "PASS" and status == "SKIP"):
                continue
            bucket[name] = status
        final = {**first, **retest}
        passed = {n for n, s in final.items() if s == "PASS"}
        failed = {n for n, s in final.items() if s == "FAIL"}
        skipped = {n for n, s in final.items() if s == "SKIP"}
        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed), failed_count=len(failed), skipped_count=len(skipped),
            passed_tests=passed, failed_tests=failed, skipped_tests=skipped,
        )
