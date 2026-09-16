import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        return "node:20-bookworm"

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    NODE_PATH=/usr/local/lib/node_modules \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates chromium \\
    && npm install -g puppeteer-core \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
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

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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

""".format(),
            ),
            File(
                ".",
                "run-mocha-headless.js",
                """#!/usr/bin/env node
const puppeteer = require('puppeteer-core');
const http = require('http');
const fs = require('fs');
const path = require('path');

const REPO_DIR = '/home/{pr.repo}';
const TEST_PATH = path.join(REPO_DIR, 'src', 'common', 'test', 'index.html');
const MOCHA_TIMEOUT_MS = 120000;

if (!fs.existsSync(TEST_PATH)) {{
  console.error('Test file not found: ' + TEST_PATH);
  console.log('SUMMARY: passes: 0, failures: 0');
  process.exit(0);
}}

function createServer(rootDir) {{
  return http.createServer((req, res) => {{
    let filePath = path.join(rootDir, decodeURIComponent(req.url.split('?')[0]));
    if (filePath.endsWith('/')) filePath += 'index.html';
    const ext = path.extname(filePath).toLowerCase();
    const mimeTypes = {{
      '.html': 'text/html', '.js': 'application/javascript',
      '.css': 'text/css', '.json': 'application/json',
      '.png': 'image/png', '.gif': 'image/gif', '.svg': 'image/svg+xml',
    }};
    fs.readFile(filePath, (err, data) => {{
      if (err) {{ res.writeHead(404); res.end('Not Found'); return; }}
      res.writeHead(200, {{ 'Content-Type': mimeTypes[ext] || 'application/octet-stream' }});
      res.end(data);
    }});
  }});
}}

(async () => {{
  const server = createServer(REPO_DIR);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const baseUrl = `http://127.0.0.1:${{port}}`;

  const browser = await puppeteer.launch({{
    executablePath: '/usr/bin/chromium',
    headless: true,
    protocolTimeout: 180000,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  }});

  const page = await browser.newPage();

  // Inject Chrome extension API mocks and Mocha runner event hooks before any scripts load.
  await page.evaluateOnNewDocument((serverBase) => {{
    // -- Chrome extension mocks --
    window.chrome = window.chrome || {{}};
    window.chrome.extension = {{
      getURL: function(url) {{
        if (url.indexOf('/common/') === 0) return serverBase + '/src' + url;
        return serverBase + url;
      }}
    }};
    window.chrome.runtime = {{
      sendMessage: function(requestObj, callback) {{
        // Mock: immediately invoke callback so async tests using
        // makeRequestToPrivilegedScript don't hang waiting for a
        // background script that doesn't exist in headless mode.
        if (callback) {{
          setTimeout(function() {{ callback(requestObj.action ? requestObj.action + '-good' : undefined); }}, 0);
        }}
      }},
      onMessage: {{
        addListener: function() {{}},
        removeListener: function() {{}}
      }}
    }};
    window.chrome.storage = {{
      sync: {{
        QUOTA_BYTES_PER_ITEM: 8192,
        get: function(keys, callback) {{
          var result = {{}};
          if (keys === null) {{
            for (var i = 0; i < localStorage.length; i++) {{
              var k = localStorage.key(i);
              try {{ result[k] = JSON.parse(localStorage.getItem(k)); }}
              catch(e) {{ result[k] = localStorage.getItem(k); }}
            }}
          }} else {{
            var keyArr = Array.isArray(keys) ? keys : (typeof keys === 'string' ? [keys] : Object.keys(keys || {{}}));
            keyArr.forEach(function(k) {{
              var val = localStorage.getItem(k);
              if (val !== null) {{
                try {{ result[k] = JSON.parse(val); }} catch(e) {{ result[k] = val; }}
              }}
            }});
          }}
          setTimeout(function() {{ callback(result); }}, 0);
        }},
        set: function(items, callback) {{
          Object.keys(items).forEach(function(k) {{ localStorage.setItem(k, JSON.stringify(items[k])); }});
          if (callback) setTimeout(callback, 0);
        }},
        remove: function(keys, callback) {{
          var keyArr = Array.isArray(keys) ? keys : [keys];
          keyArr.forEach(function(k) {{ localStorage.removeItem(k); }});
          if (callback) setTimeout(callback, 0);
        }}
      }}
    }};

    // -- Mocha runner event capture --
    // Captures ALL test results directly from Mocha runner events.
    // We use a DOMContentLoaded capture-phase listener to patch mocha.run()
    // BEFORE jQuery's ready handler (in test-run.js) calls it.
    // evaluateOnNewDocument runs before any page scripts, so our listener
    // is registered first and fires in capture phase before bubble-phase
    // jQuery handlers.
    window.__mochaResults = [];
    window.__mochaDone = false;
    window.__mochaStats = {{ passes: 0, failures: 0 }};

    function fullTitle(test) {{
      var parts = [];
      var t = test;
      while (t) {{
        if (t.title) parts.unshift(t.title);
        t = t.parent;
      }}
      return parts.join(' > ');
    }}

    document.addEventListener('DOMContentLoaded', function() {{
      if (window.mocha && window.mocha.run) {{
        var origRun = window.mocha.run.bind(window.mocha);
        window.mocha.run = function(fn) {{
          var runner = origRun(fn);
          runner.on('pass', function(test) {{
            window.__mochaResults.push('PASS: ' + fullTitle(test));
            window.__mochaStats.passes++;
          }});
          runner.on('fail', function(test) {{
            window.__mochaResults.push('FAIL: ' + fullTitle(test));
            window.__mochaStats.failures++;
          }});
          runner.on('pending', function(test) {{
            window.__mochaResults.push('SKIP: ' + fullTitle(test));
          }});
          runner.on('end', function() {{
            window.__mochaDone = true;
          }});
          return runner;
        }};
      }}
    }}, true);  // capture phase: fires before jQuery ready handlers
  }}, baseUrl);

  page.on('console', msg => console.log(msg.text()));

  const testUrl = `${{baseUrl}}/src/common/test/index.html`;
  await page.goto(testUrl, {{ waitUntil: 'domcontentloaded', timeout: 30000 }});

  // Wait for mocha runner end event
  try {{
    await page.waitForFunction(() => window.__mochaDone === true, {{
      timeout: MOCHA_TIMEOUT_MS, polling: 500
    }});
  }} catch (e) {{
    console.error('Mocha timed out after ' + MOCHA_TIMEOUT_MS + 'ms');
  }}

  // Collect results from our event-based capture
  const data = await page.evaluate(() => ({{
    results: window.__mochaResults || [],
    passes: window.__mochaStats ? window.__mochaStats.passes : 0,
    failures: window.__mochaStats ? window.__mochaStats.failures : 0,
  }}));

  data.results.forEach(r => console.log(r));
  console.log('SUMMARY: passes: ' + data.passes + ', failures: ' + data.failures);

  await browser.close();
  server.close();

  const failCount = data.results.filter(r => r.startsWith('FAIL:')).length;
  process.exit(failCount > 0 ? 1 : 0);
}})();
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
# The shared base kept full history and is pinned to nothing, so this layer owns
# the pin. --detach is required, not stylistic: the prune block in the PR
# Dockerfile ASSERTS HEAD == BASE_COMMIT rather than re-establishing it, and then
# deletes every ref. An attached HEAD would have its branch deleted out from under
# it, or would keep post-fix history reachable inside the shipped image.
cd /home/{pr.repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION ----------
# Intentionally empty. There is no package.json at this base commit -- the suite is
# a browser Mocha page (src/common/test/index.html) with its libraries vendored into
# the tree, so the repo has no era-specific install step. The single runtime
# dependency, puppeteer-core, is identical for every PR of this repo and therefore
# belongs in the shared base, where it is paid once.

# ---------- Section 3: HARD GATE ----------
# Non-tolerant, last, and deep enough to catch a partial base build: a silently
# failed `npm install -g` or a missing chromium must fail HERE, at build time,
# rather than three stages later behind an empty report that reads as "0 failures".
test -x /usr/bin/chromium
test -f /home/{pr.repo}/src/common/test/index.html
node -e "require('puppeteer-core'); console.log('DEPS_OK')"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# Baseline stage: NO patch applied. This is load-bearing, not informational --
# Report.check() step 6 (report.py:269-313) falls back to `test.run` to decide
# whether a credited test is F2P or P2P, so a baseline that fails to run silently
# corrupts the classification.
cd /home/{pr.repo}
node /home/run-mocha-headless.js
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# test.patch ONLY. The trailing `|| true` that used to sit here was removed
# deliberately: a run script has no hard gate to redeem a swallowed failure, so an
# unapplied test.patch would make this stage identical to the baseline, no test
# would transition !PASS -> PASS, and Report.check() rule 3 (report.py:217) would
# discard the instance with nothing in the log explaining why. The --3way retry is
# kept -- it is a genuine second attempt, and its failure still aborts.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
node /home/run-mocha-headless.js
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

# test.patch FIRST, then fix.patch. Order matters: the fix must land on a tree that
# already carries the new tests. Same reasoning as test-run.sh for the removed
# `|| true`.
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch
node /home/run-mocha-headless.js
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
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

{self.clear_env}
"""


@Instance.register("adam-p", "markdown-here")
class MarkdownHere(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            if line.startswith("PASS: "):
                test_name = line[len("PASS: "):]
                passed_tests.add(test_name)
            elif line.startswith("FAIL: "):
                test_name = line[len("FAIL: "):]
                failed_tests.add(test_name)
            elif line.startswith("SKIP: "):
                test_name = line[len("SKIP: "):]
                skipped_tests.add(test_name)

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
