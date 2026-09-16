import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base"


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

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
    LC_ALL=C.UTF-8 \
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

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates chromium fonts-liberation procps python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

__GLOBAL_ENV__

WORKDIR /home/

__CODE__

WORKDIR /home/__REPO__

__CLEAR_ENV__

CMD ["/bin/bash"]
"""


_CLONE_CODE = r"""RUN git clone "${REPO_URL}" /home/__REPO__"""


_COPY_CODE = r"""COPY __REPO__ /home/__REPO__"""


_PR_DOCKERFILE = r"""FROM __BASE_IMAGE__

__GLOBAL_ENV__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

WORKDIR /home/__REPO__

__HARDENING__
__CLEAR_ENV__
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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
"""


_KARMA_CI_JS = r"""var fs = require('fs');
var path = require('path');

var repo = '/home/__REPO__';
var cjsConf = path.join(repo, 'karma.conf.cjs');
var baseConf = require(fs.existsSync(cjsConf) ? cjsConf : path.join(repo, 'karma.conf.js'));

function CIReporter(baseReporterDecorator) {
  baseReporterDecorator(this);
  var self = this;

  self.onSpecComplete = function (browser, result) {
    var suite = (result.suite || []).join(' > ');
    var name = suite ? suite + ' > ' + result.description : result.description;
    var status = result.skipped ? 'SKIP' : (result.success ? 'PASS' : 'FAIL');
    self.write('KARMA_TEST|' + status + '|' + String(name).replace(/\s+/g, ' ') + '\n');
  };

  self.onBrowserLog = function (browser, log) {
    var text = String(log);
    if (text.indexOf('KARMA_LOAD_ERROR') !== -1) {
      self.write('KARMA_LOAD_ERROR|' + text.replace(/\s+/g, ' ') + '\n');
    }
  };

  self.onBrowserError = function (browser, error) {
    self.write('KARMA_BROWSER_ERROR|' + String(error).split('\n')[0] + '\n');
  };

  self.onRunComplete = function () {
    self.write('KARMA_RUN_COMPLETE\n');
  };
}
CIReporter.$inject = ['baseReporterDecorator'];

module.exports = function (config) {
  baseConf(config);

  config.set({
    basePath: repo,
    singleRun: true,
    autoWatch: false,
    colors: false,
    captureTimeout: 180000,
    browserNoActivityTimeout: 180000,
    browserDisconnectTimeout: 30000,
    browserDisconnectTolerance: 0,
    customLaunchers: {
      ChromeHeadlessCI: {
        base: 'ChromeHeadless',
        flags: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
      }
    }
  });

  config.files.unshift({
    pattern: '/home/karma-onerror-shim.js',
    included: true,
    served: true,
    watched: false
  });

  config.browsers = ['ChromeHeadlessCI'];
  config.reporters = ['ci'];
  var pluginNames = ['karma-jasmine', 'karma-jasmine-ajax', 'karma-sinon', 'karma-chrome-launcher', 'karma-webpack', 'karma-sourcemap-loader', 'karma-rollup-preprocessor'];
  var plugins = pluginNames.filter(function (name) {
    return fs.existsSync(path.join(repo, 'node_modules', name));
  });
  plugins.push({ 'reporter:ci': ['type', CIReporter] });
  config.plugins = plugins;
  config.logLevel = config.LOG_WARN;
};
"""


_KARMA_ONERROR_SHIM_JS = r"""(function (global) {
  global.onerror = function (message, source, lineno) {
    if (global.__karma__ && typeof global.__karma__.log === 'function') {
      global.__karma__.log('warn', ['KARMA_LOAD_ERROR: ' + message + ' @ ' + source + ':' + lineno]);
    }
    return true;
  };
})(window);
"""


_MOCHA_CI_REPORTER_JS = r"""'use strict';

function CIReporter(runner) {
  function title(test) {
    return String(test.fullTitle()).replace(/\s+/g, ' ').trim();
  }

  runner.on('pass', function (test) {
    process.stdout.write('MOCHA_TEST|PASS|' + title(test) + '\n');
  });

  runner.on('fail', function (test) {
    process.stdout.write('MOCHA_TEST|FAIL|' + title(test) + '\n');
  });

  runner.on('pending', function (test) {
    process.stdout.write('MOCHA_TEST|SKIP|' + title(test) + '\n');
  });

  runner.on('end', function () {
    process.stdout.write('MOCHA_RUN_COMPLETE\n');
  });
}

module.exports = CIReporter;
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git config --local advice.detachedHead false
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
test "$(git rev-parse HEAD)" = "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true

node --version
npm --version

commit_date="$(git show -s --format=%cI HEAD)"

installed=0
for attempt in 1 2 3; do
    if [ -f package-lock.json ]; then
        if npm ci --ignore-scripts --no-audit --no-fund; then
            installed=1
            break
        fi
    else
        if npm install --ignore-scripts --no-audit --no-fund --no-package-lock --before="${commit_date}"; then
            installed=1
            break
        fi
    fi
    echo "prepare: npm install attempt ${attempt} failed; retrying in 15s" >&2
    sleep 15
done
test "$installed" -eq 1

test -x node_modules/.bin/karma
test -x node_modules/.bin/mocha
node -e "require('./package.json'); require.resolve('karma'); require.resolve('karma-chrome-launcher'); require.resolve('karma-jasmine'); require.resolve('mocha'); console.log('DEPS_OK')"
"""


_TEST_BLOCK = r"""if node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 17 ? 0 : 1)"; then
    export NODE_OPTIONS=--openssl-legacy-provider
fi

set +e
node_modules/.bin/karma start /home/karma.ci.js --single-run
karma_rc=$?
node_modules/.bin/mocha --reporter /home/mocha-ci-reporter.js --timeout 30000 --retries 2 --exit 'test/unit/**/*.js'
mocha_rc=$?
module_rc=0
if [ -f test/module/test.js ]; then
    node_modules/.bin/mocha --reporter /home/mocha-ci-reporter.js --timeout 120000 --retries 2 --exit test/module/test.js
    module_rc=$?
fi
set -e

echo "STAGE_EXIT karma=${karma_rc} mocha=${mocha_rc} module=${module_rc}"
exit $(( karma_rc != 0 || mocha_rc != 0 || module_rc != 0 ))
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__TEST_BLOCK__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__TEST_BLOCK__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__TEST_BLOCK__"""


class AxiosImageBase(Image):

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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        code = _CLONE_CODE if self.config.need_clone else _COPY_CODE

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", self.dependency())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class AxiosImageDefault(Image):

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
        return AxiosImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__TEST_BLOCK__", _TEST_BLOCK)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "karma.ci.js", self._render(_KARMA_CI_JS)),
            File(".", "karma-onerror-shim.js", _KARMA_ONERROR_SHIM_JS),
            File(".", "mocha-ci-reporter.js", _MOCHA_CI_REPORTER_JS),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = (
            Image._HARDENING_BLOCK
            .replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', self.pr.base.sha)
        )

        return (
            _PR_DOCKERFILE.replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__COPY_COMMANDS__", copy_commands)
            .replace("__REPO__", self.pr.repo)
            .replace("__HARDENING__", hardening)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_KARMA_RE = re.compile(r"^KARMA_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")
_MOCHA_RE = re.compile(r"^MOCHA_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")
_LOAD_ERROR_RE = re.compile(r"^KARMA_LOAD_ERROR\|.*?/base/([^\s?'\":]+\.js)")


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

    for line in _ANSI_RE.sub("", test_log).replace("\r\n", "\n").splitlines():
        match = _KARMA_RE.match(line)
        if match:
            buckets[match.group(1)].add(f"karma::{match.group(2)}")
            continue

        match = _MOCHA_RE.match(line)
        if match:
            buckets[match.group(1)].add(f"mocha::{match.group(2)}")
            continue

        match = _LOAD_ERROR_RE.match(line)
        if match:
            failed_tests.add(f"karma::<load error> {match.group(1)}")

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


@Instance.register("axios", "axios")
class AXIOS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AxiosImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)


for _number in ("5162", "5224", "6091"):
    Instance.register("axios", _number)(AXIOS)
