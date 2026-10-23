import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_TAG = "base-9193_to_180"
NODE_IMAGE = "node:10-buster"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

CHECK_GIT_CHANGES = """#!/bin/bash
set -euo pipefail

cd "$1"

if ! git diff --quiet; then
    echo "check_git_changes: tracked files modified" >&2
    git --no-pager diff --stat >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree not clean" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
"""

APPLY_PATCHES = """#!/bin/bash
set -eo pipefail

REPO_DIR="$1"
shift

cd "$REPO_DIR"

if [ "$#" -eq 0 ]; then
    echo "apply_patches: no patch supplied" >&2
    exit 1
fi

for PATCH in "$@"; do
    if [ ! -f "$PATCH" ]; then
        echo "apply_patches: missing patch $PATCH" >&2
        exit 1
    fi
done

git reset -q
git checkout -q -- .
git clean -qfd -e node_modules

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "apply_patches: tracked tree still dirty after restore" >&2
    git status --porcelain --untracked-files=no >&2
    exit 1
fi

if git apply --whitespace=nowarn "$@"; then
    echo "apply_patches: applied without merge"
    exit 0
fi

echo "apply_patches: direct apply rejected, retrying with three-way merge" >&2
git apply -3 --whitespace=nowarn "$@"

if [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    echo "apply_patches: unresolved conflicts after three-way merge" >&2
    exit 1
fi

echo "apply_patches: applied with three-way merge"
"""

DISCOVER_TESTS = """'use strict';
var fs = require('fs');
var path = require('path');

var root = process.argv[2];

var SKIP_DIRS = {
    'node_modules': 1, '.git': 1, 'dist': 1, 'build': 1, 'coverage': 1,
    'fixtures': 1, 'fixture': 1, '__snapshots__': 1, 'results': 1,
    'sample_v2_round_trip': 1, 'node_modules_bak': 1
};
var TEST_EXT = /\\.(js|ts)$/;
var DECL = /\\.d\\.ts$/;
var SKIP_NAME = /(^|[-.])(hooks|helper|helpers|mock|mocks|stub|stubs)\\.(js|ts)$/;
var RUNNER_NAME = /^run-(smoke|tests)[-.]/;
var STRICT = /(-test|\\.test)\\.(js|ts)$/;

var found = [];

function walk(dir, inTest) {
    var entries;
    try {
        entries = fs.readdirSync(dir);
    } catch (e) {
        return;
    }
    for (var i = 0; i < entries.length; i += 1) {
        var name = entries[i];
        var full = path.join(dir, name);
        var st;
        try {
            st = fs.lstatSync(full);
        } catch (e) {
            continue;
        }
        if (st.isSymbolicLink()) {
            continue;
        }
        if (st.isDirectory()) {
            if (SKIP_DIRS[name]) {
                continue;
            }
            walk(full, inTest || name === 'test' || name === 'tests');
        } else if (st.isFile() && inTest) {
            if (!TEST_EXT.test(name) || DECL.test(name)) {
                continue;
            }
            if (SKIP_NAME.test(name) || RUNNER_NAME.test(name)) {
                continue;
            }
            found.push(path.relative(root, full).split(path.sep).join('/'));
        }
    }
}

walk(root, false);

var strict = found.filter(function (f) {
    return STRICT.test(path.basename(f));
});
var chosen = strict.length > 0 ? strict : found;
chosen.sort();

for (var j = 0; j < chosen.length; j += 1) {
    process.stdout.write(chosen[j] + '\\n');
}
"""

DETECT_RUNNER = """'use strict';
var fs = require('fs');
var path = require('path');

var root = process.argv[2];

function has(rel) {
    try {
        fs.accessSync(path.join(root, rel));
        return true;
    } catch (e) {
        return false;
    }
}

function bin(name) {
    return has(path.join('node_modules', '.bin', name));
}

var pkg = {};
try {
    pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
} catch (e) {
    pkg = {};
}

var deps = {};
var groups = [pkg.dependencies, pkg.devDependencies];
for (var g = 0; g < groups.length; g += 1) {
    var src = groups[g] || {};
    for (var k in src) {
        if (Object.prototype.hasOwnProperty.call(src, k)) {
            deps[k] = src[k];
        }
    }
}

var jestConfigured = has('jest.config.js') || has('jest.config.json') || !!pkg.jest;

if ((jestConfigured || deps.jest) && bin('jest')) {
    process.stdout.write('jest');
} else if (deps.mocha && bin('mocha')) {
    process.stdout.write('mocha');
} else if (bin('jest')) {
    process.stdout.write('jest');
} else if (bin('mocha')) {
    process.stdout.write('mocha');
} else {
    process.stdout.write('');
}
"""

EMIT_RESULTS = """'use strict';
var fs = require('fs');
var path = require('path');

var reportPath = process.argv[2];
var root = process.argv[3];
var fallbackFile = process.argv[4] || '';

function parseLoose(text) {
    var starts = [];
    if (text.charAt(0) === '{') {
        starts.push(0);
    }
    var idx = text.indexOf('\\n{');
    while (idx !== -1) {
        starts.push(idx + 1);
        idx = text.indexOf('\\n{', idx + 1);
    }
    for (var i = starts.length - 1; i >= 0; i -= 1) {
        try {
            return JSON.parse(text.slice(starts[i]));
        } catch (e) {
            continue;
        }
    }
    return null;
}

function norm(value) {
    return String(value === null || value === undefined ? '' : value).replace(/\\s+/g, ' ').trim();
}

function rel(p) {
    var raw_path = norm(p);
    if (!raw_path) {
        return '';
    }
    var out = path.isAbsolute(raw_path) ? path.relative(root, raw_path) : raw_path;
    return out.split(path.sep).join('/');
}

var emitted = 0;

function emit(file, title, status) {
    var name = rel(file || fallbackFile);
    var text = norm(title);
    var label;
    if (name && text) {
        label = name + '::' + text;
    } else if (text) {
        label = text;
    } else if (name) {
        label = name;
    } else {
        return;
    }
    process.stdout.write('TESTCASE ' + label + ' ' + status + '\\n');
    emitted += 1;
}

function jestStatus(value) {
    if (value === 'passed') {
        return 'PASSED';
    }
    if (value === 'failed') {
        return 'FAILED';
    }
    return 'SKIPPED';
}

var raw = '';
try {
    raw = fs.readFileSync(reportPath, 'utf8');
} catch (e) {
    raw = '';
}

var report = raw ? parseLoose(raw) : null;

if (report && Array.isArray(report.testResults)) {
    for (var s = 0; s < report.testResults.length; s += 1) {
        var suite = report.testResults[s];
        var file = suite.name || suite.testFilePath || '';
        var cases = Array.isArray(suite.assertionResults) ? suite.assertionResults : [];
        if (cases.length === 0 && Array.isArray(suite.testResults)) {
            cases = suite.testResults;
        }
        if (cases.length === 0) {
            var failure = norm(suite.message || suite.failureMessage || '');
            if (suite.status === 'failed' || failure) {
                emit(file, '', 'FAILED');
            }
            continue;
        }
        for (var c = 0; c < cases.length; c += 1) {
            var item = cases[c];
            var title = item.fullName;
            if (!title && Array.isArray(item.ancestorTitles)) {
                title = item.ancestorTitles.concat([item.title]).join(' ');
            }
            if (!title) {
                title = item.title;
            }
            emit(file, title, jestStatus(item.status));
        }
    }
} else if (report) {
    var buckets = [['passes', 'PASSED'], ['failures', 'FAILED'], ['pending', 'SKIPPED']];
    var sawBucket = false;
    for (var b = 0; b < buckets.length; b += 1) {
        if (Array.isArray(report[buckets[b][0]])) {
            sawBucket = true;
        }
    }
    if (sawBucket) {
        for (var k = 0; k < buckets.length; k += 1) {
            var list = Array.isArray(report[buckets[k][0]]) ? report[buckets[k][0]] : [];
            for (var n = 0; n < list.length; n += 1) {
                var entry = list[n];
                emit(entry.file || '', entry.fullTitle || entry.title, buckets[k][1]);
            }
        }
    } else if (Array.isArray(report.tests)) {
        for (var t = 0; t < report.tests.length; t += 1) {
            var test = report.tests[t];
            var status = 'PASSED';
            if (test.err && Object.keys(test.err).length > 0) {
                status = 'FAILED';
            } else if (test.pending) {
                status = 'SKIPPED';
            }
            emit(test.file || '', test.fullTitle || test.title, status);
        }
    }
}

process.stdout.write('MSWEBENCH_EMITTED ' + emitted + '\\n');
"""

TOOLCHAIN_BODY = """
export DEBIAN_FRONTEND=noninteractive

REQUIRED_BINS="git curl python3 make g++ pkg-config unzip tar gzip"

MISSING_BINS=""
for BIN in ${REQUIRED_BINS}; do
    command -v "${BIN}" >/dev/null 2>&1 || MISSING_BINS="${MISSING_BINS} ${BIN}"
done

if [ -n "$(printf '%s' "${MISSING_BINS}" | tr -d ' ')" ]; then
    echo "installing missing toolchain:${MISSING_BINS}"
    if ! apt-get update >/dev/null 2>&1; then
        sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g; s|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g; s|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' /etc/apt/sources.list
        sed -i '/-updates/d' /etc/apt/sources.list
    fi
    apt-get -o Acquire::Check-Valid-Until=false update
    apt-get install -y --no-install-recommends ${MISSING_BINS}
    rm -rf /var/lib/apt/lists/*
fi

for BIN in ${REQUIRED_BINS}; do
    command -v "${BIN}" >/dev/null 2>&1 || exit 1
done

echo "TOOLCHAIN_OK"
"""

PROVISION_BODY = """
export CI=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export PUPPETEER_SKIP_DOWNLOAD=true
export CHROMEDRIVER_SKIP_DOWNLOAD=true
export npm_config_update_notifier=false
export npm_config_fund=false
export npm_config_audit=false
export npm_config_loglevel=error
export NODE_OPTIONS="--max-old-space-size=4096"

cd "${REPO_DIR}"

ERA_CUTOFF="$(git show -s --format=%cI HEAD)"
echo "era cutoff: ${ERA_CUTOFF}"

if [ -f "${REPO_DIR}/yarn.lock" ]; then
    echo "package manager: yarn"
    yarn install --frozen-lockfile --ignore-engines --network-timeout 600000 \
        || yarn install --ignore-engines --network-timeout 600000 \
        || true
else
    echo "package manager: npm"
    npm install --before="${ERA_CUTOFF}" --no-audit --no-fund --no-package-lock \
        || npm install --no-audit --no-fund --no-package-lock \
        || true
fi

RUNNER="$(node /home/detect_runner.js "${REPO_DIR}" || echo '')"
echo "runner: ${RUNNER}"

if [ -z "${RUNNER}" ]; then
    echo "installing fallback runner from era manifest"
    MOCHA_RANGE="$(node -e 'var p=require(process.argv[1]+"/package.json");var d=Object.assign({},p.dependencies,p.devDependencies);process.stdout.write(d.mocha||"")' "${REPO_DIR}" 2>/dev/null || echo '')"
    JEST_RANGE="$(node -e 'var p=require(process.argv[1]+"/package.json");var d=Object.assign({},p.dependencies,p.devDependencies);process.stdout.write(d.jest||"")' "${REPO_DIR}" 2>/dev/null || echo '')"
    if [ -n "${JEST_RANGE}" ]; then
        npm install --no-save --no-audit --no-fund --no-package-lock "jest@${JEST_RANGE}" || true
    fi
    if [ -n "${MOCHA_RANGE}" ]; then
        npm install --no-save --no-audit --no-fund --no-package-lock "mocha@${MOCHA_RANGE}" || true
    fi
fi

node /home/discover_tests.js "${REPO_DIR}" > /home/test_targets.txt || true
echo "discovered test files: $(grep -c . /home/test_targets.txt || true)"
"""

GATE_BODY = """
for BIN in ${REQUIRED_BINS}; do
    command -v "${BIN}" >/dev/null 2>&1 || exit 1
done

cd "${REPO_DIR}"

node --version
test "$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')" -ge 10

test -d "${REPO_DIR}/node_modules"
test -n "$(ls -A "${REPO_DIR}/node_modules")"

GATE_RUNNER="$(node /home/detect_runner.js "${REPO_DIR}")"
echo "gate runner: ${GATE_RUNNER}"
test -n "${GATE_RUNNER}"

if [ "${GATE_RUNNER}" = "jest" ]; then
    test -x node_modules/.bin/jest
    node_modules/.bin/jest --version >/dev/null
else
    test -x node_modules/.bin/mocha
    node_modules/.bin/mocha --version >/dev/null
fi

node /home/discover_tests.js "${REPO_DIR}" > /home/test_targets.txt
test -s /home/test_targets.txt

node -e "require.resolve('/home/discover_tests.js'); require.resolve('/home/detect_runner.js'); require.resolve('/home/emit_results.js'); console.log('DEPS_OK')"
"""

TEST_BODY = """
export CI=true
export NODE_ENV=test
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export PUPPETEER_SKIP_DOWNLOAD=true
export CHROMEDRIVER_SKIP_DOWNLOAD=true
export npm_config_update_notifier=false
export npm_config_loglevel=error
export NODE_OPTIONS="--max-old-space-size=4096"
export FORCE_COLOR=0

cd "${REPO_DIR}"

rm -rf /home/results
mkdir -p /home/results
: > /home/emitted.txt

REPORT=/home/results/report.json

RUNNER="$(node /home/detect_runner.js "${REPO_DIR}" || echo '')"
node /home/discover_tests.js "${REPO_DIR}" > /home/test_targets.txt
TARGET_COUNT="$(grep -c . /home/test_targets.txt || true)"

echo "runner: ${RUNNER}"
echo "test files: ${TARGET_COUNT}"

echo "===== BEGIN TEST DETAIL ====="

if [ "${TARGET_COUNT}" != "0" ] && [ -n "${RUNNER}" ]; then
    TEST_INDEX=0
    while IFS= read -r TEST_FILE; do
        if [ -z "${TEST_FILE}" ]; then
            continue
        fi
        TEST_INDEX=$((TEST_INDEX + 1))
        FILE_REPORT="/home/results/report-${TEST_INDEX}.json"
        set +e
        if [ "${RUNNER}" = "jest" ]; then
            timeout --kill-after=30 900 node_modules/.bin/jest --ci --runInBand \
                --json --outputFile="${FILE_REPORT}" --runTestsByPath "${TEST_FILE}" \
                >>/home/results/runner.stdout 2>>/home/results/runner.stderr
        else
            timeout --kill-after=30 900 node_modules/.bin/mocha --reporter json --no-colors \
                "${TEST_FILE}" > "${FILE_REPORT}" 2>>/home/results/runner.stderr
        fi
        set -e
        if [ -s "${FILE_REPORT}" ]; then
            node /home/emit_results.js "${FILE_REPORT}" "${REPO_DIR}" "${TEST_FILE}" \
                | tee -a /home/emitted.txt
        fi
    done < /home/test_targets.txt
fi

echo "===== END TEST DETAIL ====="
echo "MSWEBENCH_TARGETS ${TARGET_COUNT}"

if [ "${TARGET_COUNT}" != "0" ]; then
    grep -q '^TESTCASE ' /home/emitted.txt
fi
"""

BASE_DOCKERFILE_HEAD = """# syntax=docker/dockerfile:1.6
FROM {node_image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"
"""

BASE_DOCKERFILE_ENV = """
LABEL org.opencontainers.image.title="{org}/{repo}"
LABEL org.opencontainers.image.description="multi-swe-bench base image for {org}/{repo}"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV http_proxy=${{http_proxy}}
ENV https_proxy=${{https_proxy}}
ENV no_proxy=${{no_proxy}}
ENV HTTP_PROXY=${{HTTP_PROXY}}
ENV HTTPS_PROXY=${{HTTPS_PROXY}}
ENV NO_PROXY=${{NO_PROXY}}
ENV SSL_CERT_FILE=${{CA_CERT_PATH}}
ENV REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CURL_CA_BUNDLE=${{CA_CERT_PATH}}
ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}
ENV NODE_OPTIONS="--max-old-space-size=4096"
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV CHROMEDRIVER_SKIP_DOWNLOAD=true
ENV npm_config_update_notifier=false

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/pki/ca-trust/extracted/pem /etc/ssl/certs /usr/local/share/ca-certificates; \\
    ln -sf "${{CA_CERT_PATH}}" /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf "${{CA_CERT_PATH}}" /etc/pki/tls/cert.pem; \\
    ln -sf "${{CA_CERT_PATH}}" /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf "${{CA_CERT_PATH}}" /etc/ssl/cert.pem; \\
    ln -sf "${{CA_CERT_PATH}}" /etc/ssl/ca-bundle.pem; \\
    ln -sf "${{CA_CERT_PATH}}" /usr/local/share/ca-certificates/ca-certificates.crt || true

RUN set -eux; \\
    echo "building for ${{TARGETARCH:-unknown}}"; \\
    git config --global init.defaultBranch main; \\
    git config --global advice.detachedHead false; \\
    git config --global --add safe.directory '*'
"""

PR_SCRUB = """
RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN set -eux; \\
    cd /home/{repo}; \\
    if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive 'git remote remove origin 2>/dev/null || true; git reflog expire --expire=now --all; git gc --prune=now --aggressive' || true; \\
    fi
"""


class ImageBaseNode10(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        head = BASE_DOCKERFILE_HEAD.format(
            node_image=NODE_IMAGE, org=org, repo=repo
        )
        env = BASE_DOCKERFILE_ENV.format(org=org, repo=repo)

        if self.config.need_clone:
            fetch = f'\nWORKDIR /home/\nRUN git clone "${{REPO_URL}}" /home/{repo}\n'
        else:
            fetch = f"\nWORKDIR /home/\nCOPY {repo} /home/{repo}\n"

        return (
            head
            + (f"\n{self.global_env}\n" if self.global_env else "\n")
            + env
            + fetch
            + f"\nWORKDIR /home/{repo}\n"
            + (f"\n{self.clear_env}\n" if self.clear_env else "\n")
            + '\nCMD ["/bin/bash"]\n'
        )


class ImageDefaultNode10(Image):
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
        return ImageBaseNode10(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"
        sha = self.pr.base.sha

        prepare = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "\n"
            f'REPO_DIR="{repo_dir}"\n'
            "\n"
            f'cd "{repo_dir}"\n'
            "git reset --hard\n"
            "git clean -fdx\n"
            f'bash /home/check_git_changes.sh "{repo_dir}"\n'
            f"if ! git cat-file -e {sha} 2>/dev/null; then\n"
            f"    git fetch --no-tags origin {sha}\n"
            "fi\n"
            f"git checkout --detach {sha}\n"
            f'bash /home/check_git_changes.sh "{repo_dir}"\n'
            + TOOLCHAIN_BODY
            + PROVISION_BODY
            + GATE_BODY
        )

        body = TEST_BODY

        run = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f'REPO_DIR="{repo_dir}"\n'
            'REQUIRED_BINS="git curl python3 make g++ pkg-config unzip tar gzip"\n'
            + body
        )

        test_run = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f'REPO_DIR="{repo_dir}"\n'
            'REQUIRED_BINS="git curl python3 make g++ pkg-config unzip tar gzip"\n'
            "\n"
            f'bash /home/apply_patches.sh "{repo_dir}" /home/test.patch\n'
            + body
        )

        fix_run = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f'REPO_DIR="{repo_dir}"\n'
            'REQUIRED_BINS="git curl python3 make g++ pkg-config unzip tar gzip"\n'
            "\n"
            f'bash /home/apply_patches.sh "{repo_dir}" /home/test.patch /home/fix.patch\n'
            + body
        )

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "apply_patches.sh", APPLY_PATCHES),
            File(".", "discover_tests.js", DISCOVER_TESTS),
            File(".", "detect_runner.js", DETECT_RUNNER),
            File(".", "emit_results.js", EMIT_RESULTS),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n"
            + (f"\n{self.global_env}\n" if self.global_env else "\n")
            + f"\n{copies}"
            + f"\nWORKDIR /home/{repo}\n"
            + "\nRUN bash /home/prepare.sh\n"
            + PR_SCRUB.format(repo=repo, sha=self.pr.base.sha)
            + (f"\n{self.clear_env}\n" if self.clear_env else "\n")
        )


class LighthouseInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefaultNode10(self.pr, self._config)

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
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")
        in_block = False

        for line in clean.splitlines():
            stripped = line.strip()
            if stripped == BEGIN_MARKER:
                in_block = True
                continue
            if stripped == END_MARKER:
                in_block = False
                continue
            if not in_block:
                continue

            match = case_re.match(stripped)
            if not match:
                continue

            name = match.group(1).strip()
            status = match.group(2)
            if not name:
                continue

            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


@Instance.register("GoogleChrome", "lighthouse")
@Instance.register("GoogleChrome", "lighthouse_9193_to_180")
class LIGHTHOUSE_9193_TO_180(LighthouseInstance):
    pass
