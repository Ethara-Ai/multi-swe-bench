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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return "base-era1a"

    def workdir(self) -> str:
        return "base-era1a"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Shared per-era base: installs the toolchain, clones the repo (default
        # branch, via ${REPO_URL}) and warms the COMMON npm dependencies once, so
        # every per-PR image in this era reuses them. The base pins NO commit and
        # does NOT harden -- it is shared across all era PRs; the per-PR
        # ImageDefault checks out its own ${BASE_COMMIT} in this inherited clone
        # and applies the history scrub. `# syntax` keeps DockerfileEnhancer from
        # rewriting/pinning the base clone.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y chromium && rm -rf /var/lib/apt/lists/*
RUN cd /home && npm init -y && npm install puppeteer@13 --unsafe-perm
RUN ln -sf /usr/bin/chromium /usr/bin/chromium-browser

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN npm install || true

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
                "browser-test-runner.js",
                """'use strict';
const puppeteer = require('puppeteer');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8844;
const REPO_DIR = '/home/{pr.repo}';
const TIMEOUT = 180000;

const MIME_TYPES = {{
  '.html': 'text/html', '.js': 'application/javascript',
  '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg',
  '.gif': 'image/gif', '.svg': 'image/svg+xml',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
  '.ttf': 'font/ttf', '.otf': 'font/otf',
}};

const server = http.createServer((req, res) => {{
  let filePath = path.join(REPO_DIR, decodeURIComponent(req.url.split('?')[0]));
  if (filePath.endsWith('/')) filePath += 'index.html';
  const ext = path.extname(filePath);
  const mime = MIME_TYPES[ext] || 'application/octet-stream';
  try {{
    const data = fs.readFileSync(filePath);
    res.writeHead(200, {{'Content-Type': mime}});
    res.end(data);
  }} catch (e) {{
    res.writeHead(404);
    res.end('Not found: ' + req.url);
  }}
}});

async function run() {{
  server.listen(PORT);
  const browser = await puppeteer.launch({{
    executablePath: '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
  }});
  const page = await browser.newPage();
  page.setDefaultTimeout(TIMEOUT);

  page.on('console', msg => {{
    const text = msg.text();
    if (text.startsWith('  \\u2713') || text.startsWith('  FAIL:')) {{
      process.stdout.write(text + '\\n');
    }}
  }});

  await page.evaluateOnNewDocument(() => {{
    window.__testResults = {{ passed: [], failed: [] }};
    window.__testDone = false;
    var hookInterval = setInterval(function() {{
      if (window.mocha && window.mocha.run && !window.__mochaHooked) {{
        window.__mochaHooked = true;
        var origRun = window.mocha.run.bind(window.mocha);
        window.mocha.run = function(fn) {{
          var runner = origRun(fn);
          runner.on('pass', function(test) {{
            var title = test.fullTitle();
            console.log('  \\u2713 ' + title);
            window.__testResults.passed.push(title);
          }});
          runner.on('fail', function(test, err) {{
            var title = test.fullTitle();
            console.log('  FAIL: ' + title + ' - ' + (err.message || ''));
            window.__testResults.failed.push(title);
          }});
          runner.on('end', function() {{
            window.__testDone = true;
          }});
          return runner;
        }};
        clearInterval(hookInterval);
      }}
    }}, 10);
  }});

  await page.goto('http://localhost:' + PORT + '/test/test.html', {{
    waitUntil: 'networkidle2', timeout: 60000
  }});

  try {{
    await page.waitForFunction(() => window.__testDone === true, {{ timeout: TIMEOUT }});
  }} catch (e) {{}}

  const results = await page.evaluate(() => window.__testResults);

  console.log('\\n--- RESULTS ---');
  console.log(results.passed.length + ' passing');
  console.log(results.failed.length + ' failing');
  results.passed.forEach(t => console.log('  \\u2713 ' + t));
  results.failed.forEach(t => console.log('  FAIL: ' + t));

  await browser.close();
  server.close();
  process.exit(results.failed.length > 0 ? 1 : 0);
}}

run().catch(e => {{
  console.error(e);
  server.close();
  process.exit(1);
}});
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install || true

# Generate docs/reference/data.json needed by error_helpers.js for browserify
mkdir -p docs/reference
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}}}).run(); }} catch(e) {{ require('fs').writeFileSync('docs/reference/data.json', JSON.stringify({{classitems:[], classes:{{}}, modules:{{}}}})); }}" || true

# Build p5.js for browser tests
npx grunt browserify || npx grunt build || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
timeout 1200 npx grunt mochaTest || true
timeout 1200 node /home/browser-test-runner.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch || true
npm install || true

mkdir -p docs/reference
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}}}).run(); }} catch(e) {{ require('fs').writeFileSync('docs/reference/data.json', JSON.stringify({{classitems:[], classes:{{}}, modules:{{}}}})); }}" || true
npx grunt browserify || npx grunt build || true

timeout 1200 npx grunt mochaTest || true
timeout 1200 node /home/browser-test-runner.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch /home/fix.patch || true
npm install || true

mkdir -p docs/reference
node -e "try {{ const Y = require('yuidocjs'); new Y.YUIDoc({{paths: ['src'], outdir: 'docs/reference', project: {{name: 'p5'}}}}).run(); }} catch(e) {{ require('fs').writeFileSync('docs/reference/data.json', JSON.stringify({{classitems:[], classes:{{}}, modules:{{}}}})); }}" || true
npx grunt browserify || npx grunt build || true

timeout 1200 npx grunt mochaTest || true
timeout 1200 node /home/browser-test-runner.js || true

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_files = " ".join(file.name for file in self.files())

        # The shared base already cloned the repo and warmed the common deps, so
        # this per-PR image REUSES that clone: it checks out its own ${BASE_COMMIT}
        # in /home/{repo}, COPYs the scripts, runs prepare.sh (installs the PR's
        # required deps on top of the reused node_modules + builds), then the
        # canonical Image._HARDENING_BLOCK strips origin/all refs/future history
        # (HEAD==BASE_COMMIT asserts + submodule pass). dependency() is an Image,
        # so DockerfileEnhancer returns this Dockerfile verbatim -- the checkout +
        # hardening stay as written (pinning here is correct: per-PR, not the base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


class P5JS_3089_to_1812(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()

            if line.startswith("✓ ") or line.startswith("✔ "):
                test_name = line[2:].strip()
                if test_name:
                    passed_tests.add(test_name)
            elif line.startswith("FAIL: "):
                parts = line[6:].split(" - ", 1)
                test_name = parts[0].strip()
                if test_name:
                    failed_tests.add(test_name)
            else:
                match = re.match(
                    r"^(\s*)(?:([✓✔]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$",
                    line,
                )
                if match and match.group(2) and match.group(3).strip():
                    status = match.group(2)
                    name = match.group(3).strip()
                    if status in ("✓", "✔"):
                        passed_tests.add(name)
                    elif status.endswith(")"):
                        failed_tests.add(name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
