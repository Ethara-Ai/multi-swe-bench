import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "next_4997_to_4710"
_NODE_IMAGE = "node:18.20.4-bookworm"
_BINARY_RE = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TEST_RE = re.compile(r"(KARMA|CYPRESS|MOCHA)_(TEST|RETEST)\|(PASS|FAIL|SKIP)\|(.+?)\s*$")


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
    CHROME_BIN=/usr/bin/chromium \
    CYPRESS_CACHE_FOLDER=/root/.cache/Cypress

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

RUN apt-get update && apt-get install -y --no-install-recommends \
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
cat > /home/cypress-ci-reporter.js <<'NEXT_CYPRESS_EOF'
__CYPRESS_JS__NEXT_CYPRESS_EOF
cat > /home/mocha-ci-reporter.js <<'NEXT_MOCHA_EOF'
__MOCHA_JS__NEXT_MOCHA_EOF

install_deps() {
    local mode="$1"
    node --version
    npm --version

    args=(--ignore-scripts --no-audit --no-fund --legacy-peer-deps)
    if [ "${mode}" = "base" ] && [ -f package-lock.json ]; then
        cmd=(npm ci "${args[@]}")
    else
        cmd=(npm install "${args[@]}" --no-save)
    fi

    for attempt in 1 2 3; do
        if "${cmd[@]}"; then
            break
        fi
        if [ "${attempt}" -eq 3 ]; then
            echo "deps: ${cmd[*]} failed after ${attempt} attempts" >&2
            exit 1
        fi
        echo "deps: install attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done

    if [ "$(uname -m)" = "x86_64" ]; then
        cy_ver="$(node -p "require('cypress/package.json').version")"
        cy_dir="${CYPRESS_CACHE_FOLDER:-/root/.cache/Cypress}/${cy_ver}"
        if [ ! -x "${cy_dir}/Cypress/Cypress" ]; then
            rm -rf "${cy_dir}"
            mkdir -p "${cy_dir}"
            curl -fsSL --retry 3 -o /tmp/cypress.zip "https://download.cypress.io/desktop/${cy_ver}?platform=linux&arch=x64"
            unzip -q /tmp/cypress.zip -d "${cy_dir}"
            rm -f /tmp/cypress.zip
        fi
        test -x "${cy_dir}/Cypress/Cypress"
        test "$(node -p "require('${cy_dir}/Cypress/resources/app/package.json').version")" = "${cy_ver}"
    else
        node_modules/.bin/cypress install
        ELECTRON_EXTRA_LAUNCH_ARGS="--no-sandbox --no-zygote --in-process-gpu --disable-gpu" node_modules/.bin/cypress verify
    fi
    test -x node_modules/.bin/karma
    if find components -path '*/__tests__/*' -type f \( -name '*-node.ts' -o -name '*-node.tsx' \) | grep -q .; then
        test -x node_modules/.bin/ts-mocha
    fi
    test -f cypress.config.ts
    node -e "['karma','karma-mocha','karma-webpack','cypress','mocha','ts-node','typescript'].forEach(function(m){require.resolve(m)}); console.log('DEPS_OK')"
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

_REPORTER_JS = r"""'use strict';

function specOf(test) {
  var node = test;
  while (node && !node.file && node.parent) node = node.parent;
  return String((node && node.file) || '').replace(/^.*?(components\/)/, '$1');
}

function title(test) {
  var parts = typeof test.titlePath === 'function' ? test.titlePath() : [test.fullTitle()];
  return specOf(test) + ' > ' + parts.map(function (part) {
    return String(part).replace(/\s+/g, ' ').trim();
  }).join(' > ');
}

module.exports = function (runner) {
  var prefix = __PREFIX__;
  var seen = {};
  function emit(status, test) {
    var name = title(test);
    seen[name] = (seen[name] || 0) + 1;
    if (seen[name] > 1) {
      name += ' #' + seen[name];
    }
    process.stdout.write('\n' + prefix + '|' + status + '|' + name + '\n');
  }
  runner.on('pass', function (test) { emit('PASS', test); });
  runner.on('fail', function (test) { emit('FAIL', test); });
  runner.on('pending', function (test) { emit('SKIP', test); });
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
total=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" NODE_OPTIONS=--openssl-legacy-provider timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

run_cypress() {
    local tag="$1" timeout_s="$2" spec="$3"
    CI_TEST_TAG="${tag}" timeout "${timeout_s}" node_modules/.bin/cypress run --component -b chromium \
        --reporter /home/cypress-ci-reporter.js \
        --config video=false,screenshotOnRunFailure=false,retries=2 \
        -s "${spec}" 2>&1
}

mapfile -t karma_specs < <(find components -path '*/__tests__/*' -type f -name '*-spec.js' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
total=$((total + ${#karma_specs[@]}))
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

cypress_count="$(find components -path '*/__tests__/*' -type f \( -name '*-spec.ts' -o -name '*-spec.tsx' \) | wc -l)"
echo "CYPRESS_SPEC_COUNT|${cypress_count}"
total=$((total + cypress_count))
if [ "${cypress_count}" -gt 0 ]; then
    cypress_log="$(mktemp)"
    run_cypress TEST 7200 'components/**/__tests__/**/*-spec.{ts,tsx}' | tee "${cypress_log}"
    status=${PIPESTATUS[0]}
    mapfile -t failed_specs < <(sed -n 's/^.*CYPRESS_TEST|FAIL|\([^ ]*\) > .*$/\1/p' "${cypress_log}" | LC_ALL=C sort -u)
    if ! grep -q 'CYPRESS_TEST|' "${cypress_log}"; then
        echo "CYPRESS_TEST|FAIL|components > <no results>"
        rc=1
    fi
    rm -f "${cypress_log}"
    if [ "${status}" -ne 0 ] && [ "${#failed_specs[@]}" -eq 0 ]; then
        rc=1
        echo "CYPRESS_EXIT|${status}"
    fi
    for spec in "${failed_specs[@]}"; do
        echo "CYPRESS_SPEC_RERUN|${spec}"
        run_cypress RETEST 1800 "${spec}"
        status=$?
        if [ "${status}" -ne 0 ]; then
            rc=1
            echo "CYPRESS_SPEC_EXIT|${spec}|${status}"
        fi
    done
fi

mapfile -t node_specs < <(find components -path '*/__tests__/*' -type f \( -name '*-node.ts' -o -name '*-node.tsx' \) | LC_ALL=C sort)
echo "MOCHA_SPEC_COUNT|${#node_specs[@]}"
total=$((total + ${#node_specs[@]}))
for spec in "${node_specs[@]}"; do
    out="$(TS_NODE_PROJECT=tools/tsconfig.json timeout 900 node_modules/.bin/ts-mocha --reporter /home/mocha-ci-reporter.js --timeout 120000 "${spec}" 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q 'MOCHA_TEST|' <<< "${out}"; then echo "MOCHA_TEST|FAIL|${spec} > <no results>"; fi
    if [ "${status}" -ne 0 ]; then
        rc=1
        echo "MOCHA_SPEC_EXIT|${spec}|${status}"
    fi
done

if [ "${total}" -eq 0 ]; then
    echo "NO_SPECS_FOUND"
    exit 1
fi

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
total=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" NODE_OPTIONS=--openssl-legacy-provider timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

run_cypress() {
    local tag="$1" timeout_s="$2" spec="$3"
    CI_TEST_TAG="${tag}" timeout "${timeout_s}" node_modules/.bin/cypress run --component -b chromium \
        --reporter /home/cypress-ci-reporter.js \
        --config video=false,screenshotOnRunFailure=false,retries=2 \
        -s "${spec}" 2>&1
}

mapfile -t karma_specs < <(find components -path '*/__tests__/*' -type f -name '*-spec.js' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
total=$((total + ${#karma_specs[@]}))
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

cypress_count="$(find components -path '*/__tests__/*' -type f \( -name '*-spec.ts' -o -name '*-spec.tsx' \) | wc -l)"
echo "CYPRESS_SPEC_COUNT|${cypress_count}"
total=$((total + cypress_count))
if [ "${cypress_count}" -gt 0 ]; then
    cypress_log="$(mktemp)"
    run_cypress TEST 7200 'components/**/__tests__/**/*-spec.{ts,tsx}' | tee "${cypress_log}"
    status=${PIPESTATUS[0]}
    mapfile -t failed_specs < <(sed -n 's/^.*CYPRESS_TEST|FAIL|\([^ ]*\) > .*$/\1/p' "${cypress_log}" | LC_ALL=C sort -u)
    if ! grep -q 'CYPRESS_TEST|' "${cypress_log}"; then
        echo "CYPRESS_TEST|FAIL|components > <no results>"
        rc=1
    fi
    rm -f "${cypress_log}"
    if [ "${status}" -ne 0 ] && [ "${#failed_specs[@]}" -eq 0 ]; then
        rc=1
        echo "CYPRESS_EXIT|${status}"
    fi
    for spec in "${failed_specs[@]}"; do
        echo "CYPRESS_SPEC_RERUN|${spec}"
        run_cypress RETEST 1800 "${spec}"
        status=$?
        if [ "${status}" -ne 0 ]; then
            rc=1
            echo "CYPRESS_SPEC_EXIT|${spec}|${status}"
        fi
    done
fi

mapfile -t node_specs < <(find components -path '*/__tests__/*' -type f \( -name '*-node.ts' -o -name '*-node.tsx' \) | LC_ALL=C sort)
echo "MOCHA_SPEC_COUNT|${#node_specs[@]}"
total=$((total + ${#node_specs[@]}))
for spec in "${node_specs[@]}"; do
    out="$(TS_NODE_PROJECT=tools/tsconfig.json timeout 900 node_modules/.bin/ts-mocha --reporter /home/mocha-ci-reporter.js --timeout 120000 "${spec}" 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q 'MOCHA_TEST|' <<< "${out}"; then echo "MOCHA_TEST|FAIL|${spec} > <no results>"; fi
    if [ "${status}" -ne 0 ]; then
        rc=1
        echo "MOCHA_SPEC_EXIT|${spec}|${status}"
    fi
done

if [ "${total}" -eq 0 ]; then
    echo "NO_SPECS_FOUND"
    exit 1
fi

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

    args=(--ignore-scripts --no-audit --no-fund --legacy-peer-deps)
    if [ "${mode}" = "base" ] && [ -f package-lock.json ]; then
        cmd=(npm ci "${args[@]}")
    else
        cmd=(npm install "${args[@]}" --no-save)
    fi

    for attempt in 1 2 3; do
        if "${cmd[@]}"; then
            break
        fi
        if [ "${attempt}" -eq 3 ]; then
            echo "deps: ${cmd[*]} failed after ${attempt} attempts" >&2
            exit 1
        fi
        echo "deps: install attempt ${attempt} failed; retrying in 15s" >&2
        sleep 15
    done

    if [ "$(uname -m)" = "x86_64" ]; then
        cy_ver="$(node -p "require('cypress/package.json').version")"
        cy_dir="${CYPRESS_CACHE_FOLDER:-/root/.cache/Cypress}/${cy_ver}"
        if [ ! -x "${cy_dir}/Cypress/Cypress" ]; then
            rm -rf "${cy_dir}"
            mkdir -p "${cy_dir}"
            curl -fsSL --retry 3 -o /tmp/cypress.zip "https://download.cypress.io/desktop/${cy_ver}?platform=linux&arch=x64"
            unzip -q /tmp/cypress.zip -d "${cy_dir}"
            rm -f /tmp/cypress.zip
        fi
        test -x "${cy_dir}/Cypress/Cypress"
        test "$(node -p "require('${cy_dir}/Cypress/resources/app/package.json').version")" = "${cy_ver}"
    else
        node_modules/.bin/cypress install
        ELECTRON_EXTRA_LAUNCH_ARGS="--no-sandbox --no-zygote --in-process-gpu --disable-gpu" node_modules/.bin/cypress verify
    fi
    test -x node_modules/.bin/karma
    if find components -path '*/__tests__/*' -type f \( -name '*-node.ts' -o -name '*-node.tsx' \) | grep -q .; then
        test -x node_modules/.bin/ts-mocha
    fi
    test -f cypress.config.ts
    node -e "['karma','karma-mocha','karma-webpack','cypress','mocha','ts-node','typescript'].forEach(function(m){require.resolve(m)}); console.log('DEPS_OK')"
}

if ! git diff --quiet HEAD -- package.json package-lock.json; then
    install_deps fix
fi

set +e
set -u
export TRAVIS=true
export CHROME_BIN=/usr/bin/chromium

rc=0
total=0

run_karma_spec() {
    local spec="$1" tag="$2" out status
    out="$(CI_TEST_TAG="${tag}" KARMA_SPEC="${spec}" NODE_OPTIONS=--openssl-legacy-provider timeout 900 node --max_old_space_size=8192 node_modules/.bin/karma start /home/karma.ci.js 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q "KARMA_${tag}|" <<< "${out}"; then
        echo "KARMA_${tag}|FAIL|${spec} > <no results>"
        status=1
    fi
    if grep -q "KARMA_${tag}|FAIL|" <<< "${out}"; then status=1; fi
    return "${status}"
}

run_cypress() {
    local tag="$1" timeout_s="$2" spec="$3"
    CI_TEST_TAG="${tag}" timeout "${timeout_s}" node_modules/.bin/cypress run --component -b chromium \
        --reporter /home/cypress-ci-reporter.js \
        --config video=false,screenshotOnRunFailure=false,retries=2 \
        -s "${spec}" 2>&1
}

mapfile -t karma_specs < <(find components -path '*/__tests__/*' -type f -name '*-spec.js' | LC_ALL=C sort)
echo "KARMA_SPEC_COUNT|${#karma_specs[@]}"
total=$((total + ${#karma_specs[@]}))
for spec in "${karma_specs[@]}"; do
    if ! run_karma_spec "${spec}" TEST; then
        echo "KARMA_SPEC_RERUN|${spec}"
        if ! run_karma_spec "${spec}" RETEST; then
            rc=1
            echo "KARMA_SPEC_EXIT|${spec}"
        fi
    fi
done

cypress_count="$(find components -path '*/__tests__/*' -type f \( -name '*-spec.ts' -o -name '*-spec.tsx' \) | wc -l)"
echo "CYPRESS_SPEC_COUNT|${cypress_count}"
total=$((total + cypress_count))
if [ "${cypress_count}" -gt 0 ]; then
    cypress_log="$(mktemp)"
    run_cypress TEST 7200 'components/**/__tests__/**/*-spec.{ts,tsx}' | tee "${cypress_log}"
    status=${PIPESTATUS[0]}
    mapfile -t failed_specs < <(sed -n 's/^.*CYPRESS_TEST|FAIL|\([^ ]*\) > .*$/\1/p' "${cypress_log}" | LC_ALL=C sort -u)
    if ! grep -q 'CYPRESS_TEST|' "${cypress_log}"; then
        echo "CYPRESS_TEST|FAIL|components > <no results>"
        rc=1
    fi
    rm -f "${cypress_log}"
    if [ "${status}" -ne 0 ] && [ "${#failed_specs[@]}" -eq 0 ]; then
        rc=1
        echo "CYPRESS_EXIT|${status}"
    fi
    for spec in "${failed_specs[@]}"; do
        echo "CYPRESS_SPEC_RERUN|${spec}"
        run_cypress RETEST 1800 "${spec}"
        status=$?
        if [ "${status}" -ne 0 ]; then
            rc=1
            echo "CYPRESS_SPEC_EXIT|${spec}|${status}"
        fi
    done
fi

mapfile -t node_specs < <(find components -path '*/__tests__/*' -type f \( -name '*-node.ts' -o -name '*-node.tsx' \) | LC_ALL=C sort)
echo "MOCHA_SPEC_COUNT|${#node_specs[@]}"
total=$((total + ${#node_specs[@]}))
for spec in "${node_specs[@]}"; do
    out="$(TS_NODE_PROJECT=tools/tsconfig.json timeout 900 node_modules/.bin/ts-mocha --reporter /home/mocha-ci-reporter.js --timeout 120000 "${spec}" 2>&1)"
    status=$?
    printf '%s\n' "${out}"
    if ! grep -q 'MOCHA_TEST|' <<< "${out}"; then echo "MOCHA_TEST|FAIL|${spec} > <no results>"; fi
    if [ "${status}" -ne 0 ]; then
        rc=1
        echo "MOCHA_SPEC_EXIT|${spec}|${status}"
    fi
done

if [ "${total}" -eq 0 ]; then
    echo "NO_SPECS_FOUND"
    exit 1
fi

exit "${rc}"
"""


class _NextImage_4997_TO_4710(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class NextImageBase_4997_TO_4710(_NextImage_4997_TO_4710):
    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-next_4997_to_4710"

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
            .replace("__GLOBAL_ENV__", self.global_env)
            .replace("__CLEAR_ENV__", self.clear_env)
            .replace("__CODE__", code)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class NextImageDefault_4997_TO_4710(_NextImage_4997_TO_4710):
    def dependency(self) -> Image:
        return NextImageBase_4997_TO_4710(self.pr, self._config)

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
        cypress = _REPORTER_JS.replace("__PREFIX__", "'CYPRESS_' + (process.env.CI_TEST_TAG || 'TEST')")
        mocha = _REPORTER_JS.replace("__PREFIX__", "'MOCHA_TEST'")
        prepare = self._render(_PREPARE_SH).replace("__KARMA_CI_JS__", self._render(_KARMA_CI_JS)).replace("__CYPRESS_JS__", cypress).replace("__MOCHA_JS__", mocha)
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
class NEXT_4997_TO_4710(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NextImageDefault_4997_TO_4710(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        first: dict[str, str] = {}
        retest: dict[str, dict[str, str]] = {}
        text = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in text.splitlines():
            match = _TEST_RE.search(line)
            if not match:
                continue
            runner, kind, status, name = match.groups()
            bucket = first if kind == "TEST" else retest.setdefault(runner, {})
            if bucket.get(name) == "FAIL" or (bucket.get(name) == "PASS" and status == "SKIP"):
                continue
            bucket[name] = status
        final = dict(first)
        for statuses in retest.values():
            final.update(statuses)
        passed = {n for n, s in final.items() if s == "PASS"}
        failed = {n for n, s in final.items() if s == "FAIL"}
        skipped = {n for n, s in final.items() if s == "SKIP"}
        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed), failed_count=len(failed), skipped_count=len(skipped),
            passed_tests=passed, failed_tests=failed, skipped_tests=skipped,
        )
