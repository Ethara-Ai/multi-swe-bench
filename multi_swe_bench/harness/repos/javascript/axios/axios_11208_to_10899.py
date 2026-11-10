import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# axios/axios PRs #10899 - #11208: the vitest era (axios 1.17 - 1.20 on v1.x,
# plus #10946 on v2.x). Every base commit in this range has the same test
# toolchain:
#   npm with package-lock.json (CI: `npm ci --ignore-scripts`), no
#   packageManager; vitest ^4.1.7 - ^4.1.9 with an identical vitest.config.js
#   (projects unit / browser / browser-headless); @vitest/browser-playwright
#   with playwright ^1.60; the same tests/module/{cjs,esm} and
#   tests/smoke/{cjs,esm} packages, each with its own package-lock.json.
# v2.x builds with rolldown instead of gulp + rollup and declares engines.node
# >=20, but `npm run build` is still the entry point and both are project
# dependencies, so one base serves the whole range.
#
# Test surface mirrors .github/workflows/run-ci.yml, minus the bun and deno
# smoke jobs:
#   npm run build, then vitest --project unit and --project browser-headless
#   (chromium, firefox, webkit)
#   npm pack, then the module and smoke suites against the packed axios
#   (v2.x also: npm run test:build:artifacts, run whenever the tree has it)
# CI runs the main job and the ESM suites on Node 26.x. The CJS suites run on
# Node 12 - 18 on v1.x, where they use mocha 9, and on Node 20 - 26 on v2.x,
# where they use mocha 11. mocha 9 does not load on Node >= 22, so a CJS suite
# whose mocha is 9 or older runs on Node 18, which the base installs next to
# Node 26; a newer mocha runs on Node 26.
_INTERVAL_NAME = "axios_11208_to_10899"
_LO, _HI = 10899, 11208

_NODE_IMAGE = "node:26.13.0-bookworm"
_NODE_CJS_VERSION = "18.20.8"
_NODE_CJS_SHA256 = {
    "amd64": ("x64", "27a9f3f14d5e99ad05a07ed3524ba3ee92f8ff8b6db5ff80b00f9feb5ec8097a"),
    "arm64": ("arm64", "2e3dfc51154e6fea9fc86a90c4ea8f3ecb8b60acaf7367c4b76691da192571c1"),
}
# Only used to install the browsers' system libraries in the base; each PR
# installs the browsers its own lockfile pins.
_PLAYWRIGHT_DEPS_VERSION = "1.61.1"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

# vitest reporter, printed once at the end of the run, one sorted line per
# result:
#   MSWB_VITEST <PASS|FAIL|SKIP> [<project>] <repo-relative file> > <full name>
# No durations or counts, so names are identical across stages. A file that
# fails to load, and a suite whose hook failed, are reported under their own
# names.
_VITEST_REPORTER_MJS = r"""import path from "node:path";

const STATUS = { passed: "PASS", failed: "FAIL", skipped: "SKIP", pending: "SKIP" };
const clean = s => String(s).replace(/\s+/g, " ").trim();

export default class MswbVitestReporter {
    onInit(vitest) {
        this.root = process.env.MSWB_REPO_ROOT || vitest.config.root;
    }

    onTestRunEnd(testModules, unhandledErrors) {
        const lines = [];
        for (const mod of testModules) {
            const file = path.relative(this.root, mod.moduleId).split(path.sep).join("/");
            const project = mod.project.name ? `[${clean(mod.project.name)}] ` : "";
            const kind = mod.task.meta && mod.task.meta.typecheck ? " (typecheck)" : "";
            const prefix = `${project}${file}${kind}`;
            const seen = new Map();
            let count = 0;
            for (const test of mod.children.allTests()) {
                count++;
                const base = `${prefix} > ${clean(test.fullName)}`;
                const n = (seen.get(base) || 0) + 1;
                seen.set(base, n);
                const status = STATUS[test.result().state] || "FAIL";
                lines.push(`MSWB_VITEST ${status} ${n === 1 ? base : `${base} #${n}`}`);
            }
            for (const suite of mod.children.allSuites()) {
                if (suite.errors().length) lines.push(`MSWB_VITEST FAIL ${prefix} > ${clean(suite.fullName)} <suite>`);
            }
            if (mod.errors().length) {
                lines.push(`MSWB_VITEST FAIL ${prefix}`);
            }
            else if (count === 0) {
                lines.push(`MSWB_VITEST ${mod.state() === "failed" ? "FAIL" : "PASS"} ${prefix}`);
            }
        }
        lines.sort();
        process.stdout.write(lines.join("\n") + "\n");
        process.stdout.write(`MSWB_VITEST_UNHANDLED ${(unhandledErrors || []).length}\n`);
    }
}
"""

# mocha reporter (mocha 9 loads it by path, so it uses the runner's event
# names rather than requiring mocha), one sorted line per result:
#   MSWB_MOCHA <PASS|FAIL|SKIP> [mocha] <repo-relative file> > <full title>
# A failing hook is reported under its own title.
_MOCHA_REPORTER_CJS = r"""'use strict';

const path = require('path');

const clean = (s) => String(s).replace(/\s+/g, ' ').trim();

function MswbMochaReporter(runner) {
  const root = process.env.MSWB_REPO_ROOT || process.cwd();
  const lines = [];
  const name = (test) => {
    const file = test.file ? path.relative(root, test.file).split(path.sep).join('/') : '<unknown>';
    return `[mocha] ${file} > ${clean(test.fullTitle())}`;
  };

  runner.on('pass', (test) => lines.push(`MSWB_MOCHA PASS ${name(test)}`));
  runner.on('pending', (test) => lines.push(`MSWB_MOCHA SKIP ${name(test)}`));
  runner.on('fail', (test, err) => {
    lines.push(`MSWB_MOCHA FAIL ${name(test)}`);
    process.stdout.write(`MSWB_MOCHA_ERR ${name(test)}: ${clean((err && err.message) || err).slice(0, 500)}\n`);
  });
  runner.once('end', () => {
    lines.sort();
    process.stdout.write(lines.join('\n') + '\n');
  });
}

module.exports = MswbMochaReporter;
"""

# The four sub-packages CI tests against the packed axios.
_SUITE_DIRS = "tests/module/cjs tests/module/esm tests/smoke/cjs tests/smoke/esm"


def _run_tests_sh(repo: str) -> str:
    # Identical for run.sh / test-run.sh / fix-run.sh. Every suite runs even if
    # an earlier one fails; each failure is recorded and returned at the end.
    # Setup steps report as `MSWB_SETUP <PASS|FAIL> <name>` for diagnosis
    # only; parse_log does not grade them. A suite whose setup failed
    # contributes no per-test results.
    return f"""#!/bin/bash
set -eo pipefail

REPO=/home/{repo}
cd "$REPO"
export CI=true
export MSWB_REPO_ROOT="$REPO"
NODE_CJS_BIN=/opt/node-cjs/bin
status=0

setup() {{
    local name="$1"; shift
    if "$@"; then
        echo "MSWB_SETUP PASS $name"
    else
        echo "MSWB_SETUP FAIL $name"
        status=1
        return 1
    fi
}}

suite() {{
    local dir="$1"; shift
    if (cd "$REPO/$dir" && "$@"); then
        :
    else
        status=1
    fi
}}

# PATH for a CJS (mocha) suite: Node 18 first when its mocha is 9 or older.
cjs_path() {{
    local major
    major=$(cd "$REPO/$1" && node -p "require('./node_modules/mocha/package.json').version.split('.')[0]")
    if [ "$major" -le 9 ]; then
        echo "$NODE_CJS_BIN:$PATH"
    else
        echo "$PATH"
    fi
}}

# Longer timeouts than the repo default, plus retries: several tests time
# requests or bundling against the clock and miss it intermittently when the
# machine is loaded. A test that really fails fails every attempt; tests with
# their own timeout keep it.
VITEST_ARGS="--testTimeout=60000 --hookTimeout=60000 --retry=2 --reporter=default --reporter=/home/mswb_vitest_reporter.mjs"
MOCHA_ARGS="--reporter /home/mswb_mocha_reporter.cjs"

# Same contents CI installs from `npm pack`, unpacked in place so no stage
# needs the registry. Its dependencies resolve from the root node_modules.
install_packed() {{
    local tgz
    rm -rf /tmp/mswb-pack
    mkdir -p /tmp/mswb-pack
    npm pack --ignore-scripts --pack-destination /tmp/mswb-pack > /dev/null
    tgz=$(ls /tmp/mswb-pack/*.tgz)
    for dir in {_SUITE_DIRS}; do
        rm -rf "$dir/node_modules/axios"
        mkdir -p "$dir/node_modules/axios"
        tar -xzf "$tgz" -C "$dir/node_modules/axios" --strip-components=1
    done
}}

echo "=== MSWB build"
if setup build npm run build; then
    :
fi

echo "=== MSWB suite: unit + browser-headless"
suite . npm run test:vitest:unit -- --project browser-headless $VITEST_ARGS

if node -e "process.exit(require('./package.json').scripts['test:build:artifacts'] ? 0 : 1)"; then
    echo "=== MSWB suite: build artifacts"
    suite . npm run test:build:artifacts -- $VITEST_ARGS
fi

echo "=== MSWB pack"
if setup pack install_packed; then
    echo "=== MSWB suite: module esm"
    suite tests/module/esm npm run test:module:esm -- $VITEST_ARGS

    echo "=== MSWB suite: smoke esm"
    suite tests/smoke/esm npm run test:smoke:esm:vitest -- $VITEST_ARGS

    path=$(cjs_path tests/module/cjs)
    echo "=== MSWB suite: module cjs (node $(PATH="$path" node --version))"
    suite tests/module/cjs env PATH="$path" npm run test:module:cjs -- $MOCHA_ARGS

    path=$(cjs_path tests/smoke/cjs)
    echo "=== MSWB suite: smoke cjs (node $(PATH="$path" node --version))"
    suite tests/smoke/cjs env PATH="$path" npm run test:smoke:cjs:mocha -- $MOCHA_ARGS
fi

echo "=== MSWB done status=$status"
exit "$status"
"""


class Axios11208To10899ImageBase(Image):
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
        return f"base-{_LO}_to_{_HI}"

    def workdir(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base for the whole era: keeps full history and never pins or
        # scrubs. The leading syntax directive opts out of DockerfileEnhancer,
        # so all proxy / CA / label infrastructure is written out by hand.
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

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

# --dns-result-order=ipv4first: the http adapter tests listen on and tunnel to
# "localhost", which resolves to ::1 first here but to 127.0.0.1 first on the
# CI runners; with ::1 first six of them time out.
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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    FORCE_COLOR=0 \\
    NODE_OPTIONS="--max-old-space-size=4096 --dns-result-order=ipv4first" \\
    CI=true

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
{global_block}
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

# System libraries for chromium, firefox and webkit (the set is the same for
# every playwright version the era's lockfiles pin).
RUN npx -y playwright@{_PLAYWRIGHT_DEPS_VERSION} install-deps chromium firefox webkit && \\
    rm -rf /var/lib/apt/lists/*

# Node {_NODE_CJS_VERSION} for the CJS module / smoke suites only (off PATH).
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) target="{_NODE_CJS_SHA256["amd64"][0]}"; sha="{_NODE_CJS_SHA256["amd64"][1]}" ;; \\
        arm64) target="{_NODE_CJS_SHA256["arm64"][0]}"; sha="{_NODE_CJS_SHA256["arm64"][1]}" ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/node.tgz "https://nodejs.org/dist/v{_NODE_CJS_VERSION}/node-v{_NODE_CJS_VERSION}-linux-$target.tar.gz"; \\
    echo "$sha  /tmp/node.tgz" | sha256sum -c -; \\
    mkdir -p /opt/node-cjs; \\
    tar -xzf /tmp/node.tgz -C /opt/node-cjs --strip-components=1; \\
    rm /tmp/node.tgz; \\
    /opt/node-cjs/bin/node --version

RUN git config --global --add safe.directory '*'

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class Axios11208To10899ImageDefault(Image):
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
        return Axios11208To10899ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Section 1: PIN
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

# Section 2: PROVISION
export CI=true
# Registry downloads fail transiently; retry an install up to three times.
retry() {{
    local n
    for n in 1 2 3; do
        "$@" && return 0
        echo "prepare: attempt $n failed: $*" >&2
        sleep 15
    done
    return 1
}}
retry npm ci --ignore-scripts --no-audit --no-fund
# Browsers for the playwright version this commit's lockfile pins.
retry npx playwright install --with-deps chromium firefox webkit
# Sub-packages: `npm install`, as CI does for most of them. Their lockfiles
# are not always in sync with package.json on these commits, so `npm ci`
# (which CI uses for tests/smoke/esm) can refuse. Any lockfile npm rewrites is
# restored below.
for dir in {suite_dirs}; do
    (cd "$dir" && retry npm install --ignore-scripts --no-audit --no-fund)
done
# Same build every stage runs first; done once here so a broken toolchain
# fails the image build instead of the stages.
npm run build
# Restore any tracked file the installs or the build touched (sub-package
# lockfiles) so the stage patches apply to the pristine base tree. Installed
# modules and build output are untracked and kept.
git checkout -- .

# Section 3: GATE
test -x node_modules/.bin/vitest
npx vitest --version
npx playwright --version
test -f dist/node/axios.cjs
for dir in {suite_dirs}; do
    test -d "$dir/node_modules"
done
test -f tests/module/esm/node_modules/vitest/package.json
test -f tests/smoke/esm/node_modules/vitest/package.json
test -f tests/module/cjs/node_modules/mocha/package.json
test -f tests/smoke/cjs/node_modules/mocha/package.json
/opt/node-cjs/bin/node --version
echo "DEPS_OK $(node --version) npm $(npm --version)"
""".format(repo=repo, sha=sha, suite_dirs=_SUITE_DIRS)

        run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        test_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        fix_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_vitest_reporter.mjs", _VITEST_REPORTER_MJS),
            File(".", "mswb_mocha_reporter.cjs", _MOCHA_REPORTER_CJS),
            File(".", "mswb_run_tests.sh", _run_tests_sh(repo)),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        # dependency() returns an Image, so DockerfileEnhancer never touches
        # this file: the pin and the history scrub are written out here.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
{global_block}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
{clear_block}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# MSWB_SETUP / MSWB_*_ERR / MSWB_VITEST_UNHANDLED lines are diagnostics, not
# tests, and are deliberately not matched.
_RESULT_RE = re.compile(r"^MSWB_(?:VITEST|MOCHA) (PASS|FAIL|SKIP) (\S.*?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
    for line in _ANSI_RE.sub("", test_log or "").replace("\r", "").splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.groups()
        buckets[status].add(name)

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


@Instance.register("axios", _INTERVAL_NAME)
class AXIOS_11208_TO_10899(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Axios11208To10899ImageDefault(self.pr, self._config)

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
        return _parse_mswb_log(test_log)


# ---------------------------------------------------------------------------
# Routing
#
# The JSONL carries no number_interval, so Instance.create() looks up the bare
# "axios/axios" key. This module takes that key over and sends its own range
# here; any other PR goes to whatever held the key before it was imported.
# Importing axios.py first guarantees the legacy config is at the end of that
# chain, whatever order axios/__init__.py imports the modules in.
# ---------------------------------------------------------------------------

import multi_swe_bench.harness.repos.javascript.axios.axios  # noqa: E402,F401

_FALLBACK = Instance._registry["axios/axios"]


@Instance.register("axios", "axios")
class AXIOS_11208_TO_10899_DISPATCH(Instance):
    """Router only: __new__ returns the era-appropriate instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return AXIOS_11208_TO_10899(pr, config, *args, **kwargs)
        return _FALLBACK(pr, config, *args, **kwargs)
