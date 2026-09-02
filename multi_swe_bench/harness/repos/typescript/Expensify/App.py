from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Node toolchain. Taken from the repo itself, not guessed: .nvmrc says 16.15.1
# and package.json declares engines {node: 16.15.1, npm: 8.11.0}. The image ships
# exactly npm 8.11.0, so no nvm layer is needed -- the previous revision of this
# file booted node:18 and then tried to `nvm use` inside each run script, which
# silently fell back to Node 18 whenever sourcing nvm failed.
NODE_IMAGE = "node:16.15.1"

# The repo's own test script is `"test": "TZ=utc jest"`. TZ=utc is NOT optional
# here: PR 24446 is a DateUtils/timezone refactor, so the container's local
# timezone would otherwise decide the result of the very tests being graded.
#
# --ci        : never write new snapshots, fail instead.
# --silent    : keep console.log noise out of the graded log.
# --json      : machine-readable results; see jest-report.js for why.
TEST_CMD = (
    "TZ=utc node_modules/.bin/jest --ci --silent --json "
    "--outputFile=/tmp/jest-results.json; "
    "node /home/jest-report.js /tmp/jest-results.json"
)


class ExpensifyAppImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        # Deliberately NOT `git clone`. Expensify/App has ~2.76 million objects;
        # a full clone measured 2.8 GB and over 15 minutes, and every byte of it
        # would land in the base image. Initialising an empty repo and fetching
        # the single graded commit at depth 1 measured 88 MB in 8 seconds and
        # leaves exactly the tree the harness needs.
        #
        # need_clone is ignored on purpose: the COPY path would move the same
        # 2.8 GB through the build context instead.
        fetch = f"""RUN set -eux; \\
    mkdir -p /home/{repo}; \\
    cd /home/{repo}; \\
    git init -q .; \\
    git remote add origin "${{REPO_URL}}"; \\
    git fetch -q --depth 1 origin "${{BASE_COMMIT}}"; \\
    git checkout -q --detach FETCH_HEAD"""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT={sha}

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

# CI=true does double duty. jest treats it as CI mode, and the repo's own
# scripts/postInstall.sh skips a `cd desktop && npm install` step when CI is set
# -- a step that installs a second dependency tree the tests never load.
ENV NODE_ENV=test \\
    CI=true \\
    npm_config_fund=false \\
    npm_config_audit=false
WORKDIR /home/

# No apt-get. node:16.15.1 is built on buildpack-deps:bullseye, which already
# ships git, curl, ca-certificates, patch and coreutils -- and bullseye is now
# past EOL, so `apt-get update` returns 404 for it. Asserting beats installing.
# jq is NOT in this image and is NOT needed; the previous revision apt-installed
# it. csplit is used by apply_patch.sh.
RUN set -eux; \\
    for t in git curl patch csplit node npm; do \\
        command -v "$t" >/dev/null 2>&1 || {{ echo "missing required tool: $t"; exit 1; }}; \\
    done; \\
    node --version; \\
    npm --version

# package.json pulls two dependencies over ssh://git@github.com/, which cannot
# authenticate inside a container. Rewrite to https so npm can fetch them.
RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\
    git config --global url."https://github.com/".insteadOf "git@github.com:"

# This repo's install is large and slow; the defaults time out mid-way and npm
# reports it as a network failure. Verified: the first attempt died with
# ERR_SOCKET_TIMEOUT before these were raised.
#
# maxsockets is lowered from the default 15 because that default is what makes
# the failure likely: 15 parallel connections pulling a 1548-package tree
# saturates a constrained link, and ONE socket timeout aborts the entire
# `npm ci`. Fewer sockets is slower per request and far more likely to finish.
RUN npm config set fetch-timeout 900000 && \\
    npm config set fetch-retries 8 && \\
    npm config set fetch-retry-mintimeout 20000 && \\
    npm config set fetch-retry-maxtimeout 180000 && \\
    npm config set maxsockets 6

{fetch}

WORKDIR /home/{repo}

RUN set -eux; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"

{self.clear_env}

CMD ["/bin/bash"]
"""


class ExpensifyAppImageDefault(Image):
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
        return ExpensifyAppImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
# Assert the working tree is pristine. `git reset --hard` restores tracked files
# but does NOT remove stray untracked ones, and the Dockerfile's HEAD/refs asserts
# only prove WHICH commit is checked out -- a dirty tree satisfies all of them.
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
""",
            ),
            File(
                ".",
                "strip-unfetchable-deps.js",
                """// Remove dependencies whose SOURCE has rotted since this commit was authored
// (2023-08). These are build-time blockers, not behaviour changes -- npm ci
// aborts on the first one and no test ever runs.
//
//   react-native-flipper
//     devDependency pinned to https://gitpkg.now.sh/facebook/flipper/... , a
//     third-party proxy that serves GitHub subdirectories as npm packages. It
//     now redirects to gitpkg.vercel.app and answers 402 Payment Required
//     (verified). The package is a Flipper debugger integration; the only
//     reference in the tree is config/webpack/webpack.common.js, and jest never
//     loads webpack config -- so the test suite is unaffected.
//
// Both files are edited so `npm ci` still sees package.json and package-lock.json
// in agreement (npm ci aborts if they disagree). prepare.sh restores both from
// git immediately after installing, so the GRADED tree is byte-identical to
// base.sha -- only node_modules/, which is gitignored, survives.
const fs = require('fs');

const DROP = ['react-native-flipper'];

const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
for (const name of DROP) {
    for (const section of ['dependencies', 'devDependencies', 'optionalDependencies']) {
        if (pkg[section]) delete pkg[section][name];
    }
}
fs.writeFileSync('package.json', JSON.stringify(pkg, null, 4) + '\\n');

const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
if (lock.packages) {
    const root = lock.packages[''] || {};
    for (const name of DROP) {
        for (const section of ['dependencies', 'devDependencies', 'optionalDependencies']) {
            if (root[section]) delete root[section][name];
        }
        delete lock.packages['node_modules/' + name];
    }
    // Other packages declare these as peerDependencies; leaving those in place
    // makes npm try to satisfy them and reach for the dead URL again.
    for (const key of Object.keys(lock.packages)) {
        const entry = lock.packages[key];
        if (entry && entry.peerDependencies) {
            for (const name of DROP) delete entry.peerDependencies[name];
        }
    }
}
if (lock.dependencies) {
    for (const name of DROP) delete lock.dependencies[name];
}
fs.writeFileSync('package-lock.json', JSON.stringify(lock, null, 2) + '\\n');

console.log('strip-unfetchable-deps: removed ' + DROP.join(', '));
""",
            ),
            File(
                ".",
                "jest-report.js",
                """// Turn jest's --json output into one flat line per test.
//
// The human-readable reporter cannot be parsed safely here. Its suite headers
// ("PASS tests/unit/Foo.js") look exactly like result lines, and its per-test
// lines carry only the test's own title -- so two tests with the same title in
// different files collapse into one name, merging a pass with a failure. This
// suite has 46 files and 618 tests, so that is a real collision, not a
// theoretical one.
//
// The emitted name is `<file> > <describe...> > <title>`, which is unique and,
// crucially, STABLE across the run/test/fix stages -- that stability is what
// lets the harness classify a test as fail-to-pass at all.
const fs = require('fs');

const path = process.argv[2];
if (!path || !fs.existsSync(path)) {
    // Not fatal: jest can die before writing the file (an OOM, a config error).
    // Say so loudly and exit 0 so the stage still produces a log.
    console.log('jest-report: no results file at ' + path + ' -- jest produced no output');
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('jest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED'};

for (const suite of report.testResults || []) {
    // Absolute container path -> repo-relative, so names do not embed /home/App.
    let file = String(suite.name || '').replace(/\\\\/g, '/');
    const marker = '/{repo}/';
    const at = file.indexOf(marker);
    if (at !== -1) file = file.slice(at + marker.length);

    // A suite that fails to even load reports zero assertions. Emit one FAILED
    // line for the file so the stage does not silently lose it.
    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of results) {
        const parts = [file].concat(a.ancestorTitles || [], [a.title]);
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + parts.join(' > '));
    }
}

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
""".replace("{repo}", self.pr.repo),
            ),
            File(
                ".",
                "apply_patch.sh",
                r"""#!/bin/bash
# Apply one patch as completely as possible, then ALWAYS exit 0. The caller must
# reach jest no matter how patching went: a stage that dies while patching
# reports zero tests, which the harness cannot tell apart from "the fix does not
# work". Whole-patch fast path first; per-file cascade only when something
# rejects, so one unappliable file cannot take the gold tests down with it.

patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            # From HEAD, not the index: `git apply --3way` stages what it merges,
            # so `git checkout -- <path>` would restore the half-applied version.
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    # Exiting 0 stays deliberate -- the caller must still reach jest. But a
    # patch that did not fully apply must not be discoverable only by a human
    # reading the log. Drop a marker the run-scripts turn into a loud banner.
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"

# See strip-unfetchable-deps.js. Runs BEFORE install and is undone after it.
node /home/strip-unfetchable-deps.js

# --ignore-scripts is not caution, it is necessary: the `shellcheck` package's
# postinstall downloads a binary that no longer extracts ("xz: File format not
# recognized", verified), and it aborts the whole install. Skipping every
# package lifecycle script sidesteps that class of rot wholesale.
#
# But the repo's OWN postinstall matters -- scripts/postInstall.sh runs
# patch-package against 11 patches under patches/, several of them against
# react-native 0.72.3 itself. So it is run explicitly, immediately after.
# Retried in a loop rather than relying on npm's own fetch-retries. Those retry
# an INDIVIDUAL request; a socket timeout aborts the whole install, and the run
# is then lost even though most of the 1548 packages already downloaded.
# Verified: a build died at ERR_SOCKET_TIMEOUT on @webassemblyjs/ast after 316s.
#
# Re-running inside the SAME layer is what makes this cheap -- npm's HTTP cache
# under /root/.npm survives between attempts, so each retry starts further along
# and later attempts add --prefer-offline to lean on it. A retry across docker
# builds would throw that cache away with the failed layer.
ok=0
for attempt in 1 2 3 4; do
    if [ "$attempt" -eq 1 ]; then
        extra=""
    else
        extra="--prefer-offline"
        echo "prepare: npm ci attempt $attempt (retrying with cache)"
    fi
    if npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund $extra; then
        ok=1
        echo "prepare: npm ci succeeded on attempt $attempt"
        break
    fi
    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"
    sleep 15
done
if [ "$ok" -ne 1 ]; then
    echo "prepare: npm ci FAILED after 4 attempts"
    exit 1
fi

npx --no-install patch-package

# Undo the package.json / package-lock.json edits. node_modules/ is gitignored
# and survives, so the graded tree is byte-identical to base.sha while still
# having its dependencies installed.
git checkout -- package.json package-lock.json
git clean -fdq
bash /home/check_git_changes.sh

node --version
npm --version
node_modules/.bin/jest --version
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# No `set -e`: a non-zero jest exit is the NORMAL outcome of a stage whose tests
# fail, and the log is the deliverable.
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # A PR layer is COPYs + one `RUN bash /home/prepare.sh`, nothing else --
        # no FROM of a runtime, no clone, no apt, no history scrub. All of that
        # belongs to the base image, which already hardens and asserts
        # HEAD/refs/remotes after checkout.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Expensify", "App")
class ExpensifyApp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExpensifyAppImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Parses the flat lines emitted by jest-report.js, NOT jest's own
        # console reporter -- see that file for why the reporter output is
        # unsafe to parse here.
        #
        # Anchored at the start of the line so a failure DIFF that happens to
        # contain the word PASSED cannot be mistaken for a result.
        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S.*)$")

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A name may live in only one bucket; failure wins.
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
