"""reveal.js harness -- Grunt era, routing key `reveal_js_0_to_2651`.

Superseded for NEW data by reveal_js_0_to_2745 (the delivered bundles put the
Grunt/Gulp boundary at #2745/#2746, matching reveal.js's v4.0 switch), but the
key stays live for any existing jsonl that references it.

Hardening split (see image.py) -- identical contract to reveal_js_0_to_2745:
ImageBase is SHARED across every PR on this key, so it opts out of
DockerfileEnhancer via `# syntax=docker/dockerfile:1.6` (image.py:281) and
keeps full git history with light hardening only. Letting the enhancer inject
Image._HARDENING_BLOCK here would pin the shared tag to whichever PR built it
first and break `git checkout` for all the others. ImageDefault is PER-PR, so
it emits the canonical block by hand after pinning ${BASE_COMMIT}, and declares
`ARG BASE_COMMIT="<sha>"` with a default because per-PR images receive no build
args (build_dataset.py:614-620).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# GitHub's exact casing, and the path the clone + WORKDIR create.
REPO_DIR = "reveal.js"

# node:10-buster predates Debian's move of buster to archive.debian.org, and
# Image.DEPRECATED_DEBIAN_IMAGES only matches bare `debian:buster` -- so
# _get_apt_update_command does NOT rewrite the sources for this tag and the
# rewrite has to stay hand-written here or `apt-get update` 404s.
_APT_COMMAND = (
    "RUN sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list \\\n"
    "    && sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list \\\n"
    "    && sed -i '/buster-updates/d' /etc/apt/sources.list \\\n"
    "    && apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    git \\\n"
    "    chromium \\\n"
    "    fonts-liberation \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    wget \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)

# Configure git globally BEFORE cloning; the arm64 half of a multi-arch build
# runs under QEMU where a stalled transfer easily trips libcurl's low-speed
# timeout and dies with `gnutls_handshake() failed`.
_GIT_RESILIENCY = (
    "RUN git config --global http.version HTTP/1.1 \\\n"
    "    && git config --global http.postBuffer 1048576000 \\\n"
    "    && git config --global http.lowSpeedLimit 0 \\\n"
    "    && git config --global http.lowSpeedTime 999999 \\\n"
    "    && git config --global core.compression 0 \\\n"
    "    && git config --global submodule.fetchJobs 1"
)

# LIGHT hardening only -- drop the origin remote so the image carries no
# upstream to re-fetch from. The canonical Image._HARDENING_BLOCK deliberately
# does NOT run here; see module docstring.
_LIGHT_HARDENING = (
    "RUN git remote remove origin 2>/dev/null || true; \\\n"
    "    git config --local fetch.recurseSubmodules false; \\\n"
    '    git config --local remote.pushDefault ""; \\\n'
    "    git config --local gc.auto 0"
)


class ImageBase(Image):

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
        # node:12-bullseye - reveal.js 2.x era's Gruntfile targets `grunt-contrib-qunit
        # ~0.2.2`, whose phantomjs 1.9.20 dep has no arm64 binary and whose
        # install.js aborts on non-x86_64. Rather than pin the whole image to
        # linux/amd64 (native-arch chromium is available), we replace the grunt
        # qunit task at runtime with a puppeteer-based runner (see run-qunit.js
        # File below) that drives the same test/*.html files against the
        # apt-installed chromium binary. Node 12 stays because grunt 0.4.5 still
        # loads its Gruntfile cleanly under it. Bullseye stays because its apt
        # repos are still on deb.debian.org (buster/stretch/jessie moved to
        # archive.debian.org and the framework's default apt path breaks there).
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        if self.config.need_clone:
            # Retry x3 so one flaky handshake does not discard the build. The
            # bare `git clone "${REPO_URL}" /home/<repo>` form is preserved
            # inside the loop so the reference-format marker still matches.
            fetch = (
                "RUN for i in 1 2 3; do \\\n"
                f'        git clone "${{REPO_URL}}" /home/{REPO_DIR} && break; \\\n'
                '        echo "clone attempt $i failed; retrying"; \\\n'
                f"        rm -rf /home/{REPO_DIR}; \\\n"
                "        sleep 10; \\\n"
                "    done; \\\n"
                f"    test -d /home/{REPO_DIR}/.git"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git chromium ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install -g grunt-cli

{code}

{self.clear_env}

"""


class ImageDefault(Image):

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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
                "run-qunit.js",
                """#!/usr/bin/env node
const puppeteer = require('puppeteer');
const path = require('path');
const fs = require('fs');

const TEST_DIR = 'test';
const PER_FILE_TIMEOUT_MS = 30000;
const CHROMIUM = process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium';

(async () => {
    const files = fs.readdirSync(TEST_DIR).filter(f => f.endsWith('.html'));
    if (files.length === 0) {
        console.log('run-qunit: no test/*.html files found');
        process.exit(0);
    }

    let browser;
    try {
        browser = await puppeteer.launch({
            executablePath: CHROMIUM,
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
            headless: true,
        });
    } catch (e) {
        console.log('run-qunit: FAILED to launch chromium at ' + CHROMIUM + ': ' + e.message);
        process.exit(1);
    }

    let exitCode = 0;
    for (const file of files) {
        const relFile = TEST_DIR + '/' + file;
        const fileUrl = 'file://' + path.resolve(TEST_DIR, file);
        let result;
        let page;
        try {
            page = await browser.newPage();
            page.setDefaultTimeout(PER_FILE_TIMEOUT_MS);
            await page.goto(fileUrl, { waitUntil: 'load', timeout: PER_FILE_TIMEOUT_MS });
            result = await page.evaluate((timeoutMs) => {
                return new Promise((resolve) => {
                    const deadline = Date.now() + timeoutMs - 2000;
                    (function poll() {
                        const el = document.getElementById('qunit-testresult');
                        if (el && el.textContent) {
                            const m = el.textContent.match(/(\\d+)\\s+(?:assertions?|tests?)\\s+of\\s+(\\d+)\\s+passed,\\s*(\\d+)\\s+failed/);
                            if (m) {
                                return resolve({
                                    passed: parseInt(m[1], 10),
                                    total: parseInt(m[2], 10),
                                    failed: parseInt(m[3], 10),
                                });
                            }
                        }
                        if (Date.now() > deadline) {
                            return resolve({ error: (typeof QUnit === 'undefined') ? 'QUnit_not_loaded' : 'timeout' });
                        }
                        setTimeout(poll, 100);
                    })();
                });
            }, PER_FILE_TIMEOUT_MS);
        } catch (e) {
            result = { error: e.message };
        } finally {
            if (page) { try { await page.close(); } catch (_) {} }
        }

        const failed = (result && result.failed) || 0;
        const total = (result && result.total) || 0;
        const passed = (result && result.passed) || 0;

        if (result && !result.error && failed === 0 && total > 0) {
            console.log('Testing ' + relFile + ' ..... OK (' + passed + '/' + total + ')');
        } else {
            const reason = (result && result.error) || (failed + ' failed');
            console.log('Testing ' + relFile + ' ..... FAILED (' + passed + '/' + total + ') [' + reason + ']');
            exitCode = 1;
        }
    }

    try { await browser.close(); } catch (_) {}
    process.exit(exitCode);
})().catch((e) => {
    console.error('run-qunit: uncaught: ' + e.message);
    process.exit(1);
});
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Build-time only. Pins the working tree to THIS PR's ${{BASE_COMMIT}} -- not a
# baked-in literal sha -- so the value the hardening block later asserts
# against is the same one used here.
set -e

cd /home/{repo_dir}
git reset --hard
git checkout ${{BASE_COMMIT}}
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
export CHROMIUM_BIN=$PUPPETEER_EXECUTABLE_PATH

npm install --no-audit --no-fund || true

npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
export CHROMIUM_BIN=$PUPPETEER_EXECUTABLE_PATH
# grunt from PATH (base image installs grunt-cli globally). NODE_PATH lets
# /home/run-qunit.js require('puppeteer') resolve to /home/reveal.js/node_modules/,
# since Node's module search starts from the SCRIPT's dir (/home/), not CWD.
export NODE_PATH=/home/reveal.js/node_modules
grunt jshint 2>&1 || true
node /home/run-qunit.js 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
export CHROMIUM_BIN=$PUPPETEER_EXECUTABLE_PATH
# PhantomJS 2.1.1 (grunt-contrib-qunit 1.x, what reveal.js v3.x pins) is linked
# against OpenSSL 1.0. Against a 1.1.1 runtime it reads /etc/ssl/openssl.cnf,
# fails to dlopen libssl_conf.so for the `ssl_conf` module, and aborts on the
# FIRST test file -- grunt prints "Testing <file>" but no browser ever starts,
# so every stage captures ZERO tests. An empty OPENSSL_CONF skips that config.
export OPENSSL_CONF=/dev/null
git apply --reject --whitespace=nowarn /home/test.patch || true
# --ignore-scripts skips node-sass@0.9.x's node-gyp postinstall which hangs indefinitely under emulation.
timeout 300 npm install --ignore-scripts --no-audit --no-fund 2>&1 || true
npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0 2>&1 || true
# grunt from PATH (base image installs grunt-cli globally). NODE_PATH lets
# /home/run-qunit.js require('puppeteer') resolve to /home/reveal.js/node_modules/,
# since Node's module search starts from the SCRIPT's dir (/home/), not CWD.
export NODE_PATH=/home/reveal.js/node_modules
grunt jshint 2>&1 || true
node /home/run-qunit.js 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=$(which chromium || which chromium-browser || echo /usr/bin/chromium)
export CHROMIUM_BIN=$PUPPETEER_EXECUTABLE_PATH
# PhantomJS 2.1.1 (grunt-contrib-qunit 1.x, what reveal.js v3.x pins) is linked
# against OpenSSL 1.0. Against a 1.1.1 runtime it reads /etc/ssl/openssl.cnf,
# fails to dlopen libssl_conf.so for the `ssl_conf` module, and aborts on the
# FIRST test file -- grunt prints "Testing <file>" but no browser ever starts,
# so every stage captures ZERO tests. An empty OPENSSL_CONF skips that config.
export OPENSSL_CONF=/dev/null
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch || true
# --ignore-scripts skips node-sass@0.9.x's node-gyp postinstall which hangs indefinitely under emulation.
timeout 300 npm install --ignore-scripts --no-audit --no-fund 2>&1 || true
npm install --no-save --no-audit --no-fund --ignore-scripts puppeteer@10.4.0 2>&1 || true
# grunt from PATH (base image installs grunt-cli globally). NODE_PATH lets
# /home/run-qunit.js require('puppeteer') resolve to /home/reveal.js/node_modules/,
# since Node's module search starts from the SCRIPT's dir (/home/), not CWD.
export NODE_PATH=/home/reveal.js/node_modules
grunt jshint 2>&1 || true
node /home/run-qunit.js 2>&1 || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{REPO_DIR}

{copy_commands}RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete every ref, expire the
        # reflog, gc/repack, drop alternates, then assert; plus the submodule
        # strip). Concatenated raw rather than interpolated through the f-string
        # above so its ${BASE_COMMIT} and %(refname) tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("hakimel", "reveal_js_0_to_2651")
class REVEAL_JS_0_TO_2651(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        """Parse grunt-contrib-qunit test output.

        grunt-contrib-qunit v0.x-v1.x (PhantomJS) output:
            Testing test/test.html ............OK
            >> 12 assertions of 12 passed, 0 failed.

        grunt-contrib-qunit v3.x (headless Chrome, same as gulp era) output:
            ✔ test/test.html [158/158] in 450ms
            ! test/test-foo.html [5/8] in 120ms
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        re_phantom_ok = re.compile(
            r"Testing\s+(\S+)\s+(?:\.+\s*)?(?:OK|ok)"
        )
        re_phantom_fail = re.compile(
            r"Testing\s+(\S+)\s+(?:\.+\s*)?(?:FAILED|failed|FAIL|fail)"
        )
        re_v3_pass = re.compile(
            r"^[✔✓]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )
        re_v3_fail = re.compile(
            r"^[!✗✕]\s+(.+?)\s+\[(\d+)/(\d+)\]\s+in\s+\d+ms$"
        )

        for line in log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_v3_pass.match(line)
            if m:
                # Trust the RATIO, not the glyph. Crediting on the check mark
                # alone banks a partial failure ("✔ test/test.html [155/158]")
                # as a full pass, which is a free F2P the moment the assertion
                # count moves.
                if m.group(2) == m.group(3):
                    passed_tests.add(m.group(1))
                else:
                    failed_tests.add(m.group(1))
                continue

            m = re_v3_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_phantom_ok.search(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_phantom_fail.search(line)
            if m:
                failed_tests.add(m.group(1))
                continue

        for test in failed_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
