import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files


def _target_test_files(test_patch: str) -> str:
    """Space-joined `test/**/*.js` spec files touched by this bundle's test.patch.

    Passing these to `vitest run` isolates the run to the bundle's own tests, so a
    genuine fail->pass isn't drowned out by the full browser suite's unrelated
    (often GPU/network-dependent) failures. Non-spec artifacts (screenshots,
    .ts type tests, manual-test html, visual runner infra) are excluded. Empty
    string means "no targetable spec" -> fall back to the whole project.
    """
    specs = [
        f
        for f in get_modified_files(test_patch or "")
        if f.startswith("test/")
        and f.endswith(".js")
        and "/screenshots/" not in f
        and "/manual-test" not in f
        and "visualTestList" not in f
        and "visualTestRunner" not in f
    ]
    return " ".join(specs)


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

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

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

RUN apt-get update && apt-get install -y chromium chromium-driver libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 && rm -rf /var/lib/apt/lists/*
ENV CHROME_BIN=/usr/bin/chromium
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

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
        targets = _target_test_files(self.pr.test_patch)
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
                "patch_vitest.js",
                r"""const fs = require('fs');
const path = require('path');

let f = null;
if (fs.existsSync('vitest.workspace.mjs')) f = 'vitest.workspace.mjs';
else if (fs.existsSync('vitest.config.js')) f = 'vitest.config.js';
else process.exit(0);

let c = fs.readFileSync(f, 'utf8');

let vitestVersion = '0.0.0';
try {
  const pkg = JSON.parse(fs.readFileSync('node_modules/vitest/package.json', 'utf8'));
  vitestVersion = pkg.version;
} catch(e) {}
const major = parseInt(vitestVersion.split('.')[0], 10);
const needsFactory = major >= 3;

if (f === 'vitest.config.js' && c.includes('projects')) {
  if (needsFactory) {
    // vitest >=3: modify vitest.config.js in-place (replace webdriverio with playwright)
    c = c.replace(/import\s*\{[^}]*\}\s*from\s*['"]@vitest\/browser-webdriverio['"];?\n?/g, '');
    c = `import { playwright } from '@vitest/browser-playwright';\n${c}`;
    c = c.replace(/provider:\s*webdriverio\s*\(\s*\{\s*\}\)/g,
      `provider: playwright({ launch: { executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'] } })`);
    c = c.replace(/provider:\s*webdriverio\s*\(/g, `provider: playwright(`);
    c = c.replace(/headless:\s*false/g, 'headless: true');
    c = c.replace(/name:\s*['\"]chrome['\"]/g, "name: 'chromium'");
    c = c.replace(/\{\s*browser:\s*['\"]chrome['\"]\s*\}/g, "{ browser: 'chromium' }");
    if (!c.includes('headless: true')) {
      c = c.replace(/(browser:\s*\{)/, '$1\n    headless: true,');
    }
    if (!c.includes('screenshotFailures')) {
      c = c.replace(/(headless:\s*true)/, '$1,\n    screenshotFailures: false');
    }
    // Inject flaky test excludes
    const flakyExcludes = [
      './test/unit/visual/**',
      './test/unit/io/files.js',
      './test/unit/io/loadModel.js',
      './test/unit/io/saveTable.js',
      './test/unit/accessibility/outputs.js',
      './test/unit/core/environment.js',
      './test/unit/type/loading.js',
      './test/unit/type/attributes.js',
      './test/unit/type/p5.Font.js',
      './test/unit/webgl/p5.Framebuffer.js',
      './test/unit/webgl/p5.Camera.js',
      './test/unit/webgl/p5.Texture.js',
      './test/unit/image/downloading.js',
      './test/unit/webgl/p5.Shader.js'
    ];
    const fExcludeStr = flakyExcludes.map(e => `'${e}'`).join(',\n        ');
    c = c.replace(/(exclude:\s*\[)([^\]]*?)(\])/gs, (match, open, items, close) => {
      const existing = items.trim();
      const sep = existing.endsWith(',') || !existing ? '' : ',';
      return `${open}${items}${sep}\n        ${fExcludeStr}\n      ${close}`;
    });
    fs.writeFileSync(f, c);
  } else {
    // vitest <3: convert vitest.config.js to vitest.workspace.mjs
    const browserBlock = `browser: {
        enabled: true,
        name: 'chromium',
        provider: 'playwright',
        headless: true,
        screenshotFailures: false,
        providerOptions: {
          launch: {
            executablePath: '/usr/bin/chromium',
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
          }
        }
      }`;
    const workspace = `import { defineWorkspace, configDefaults } from 'vitest/config';
import vitePluginString from 'vite-plugin-string';

const plugins = [
  vitePluginString({
    include: ['src/webgl/shaders/**/*'],
    compress: false,
  })
];

export default defineWorkspace([
  {
    plugins,
    publicDir: './test',
    test: {
      name: 'unit-tests',
      root: './',
      include: ['./test/unit/**/*.js'],
      exclude: [
        './test/unit/spec.js',
        './test/unit/assets/**/*',
        './test/unit/visual/**',
        './test/unit/webgpu/*.js',
        './test/types/**/*',
        './test/unit/io/files.js',
        './test/unit/io/loadModel.js',
        './test/unit/io/saveTable.js',
        './test/unit/accessibility/outputs.js',
        './test/unit/core/environment.js',
        './test/unit/type/loading.js',
        './test/unit/type/attributes.js',
        './test/unit/type/p5.Font.js',
        './test/unit/webgl/p5.Framebuffer.js',
        './test/unit/webgl/p5.Camera.js',
        './test/unit/webgl/p5.Texture.js',
        './test/unit/image/downloading.js',
        './test/unit/webgl/p5.Shader.js'
      ],
      testTimeout: 3000,
      globals: true,
      ${browserBlock},
      fakeTimers: {
        toFake: [...(configDefaults.fakeTimers.toFake ?? []), 'performance']
      }
    }
  }
]);`;
    fs.writeFileSync('vitest.workspace.mjs', workspace);
    try { fs.unlinkSync('vitest.config.js'); } catch(e) {}
  }
  process.exit(0);
}

c = c.replace(/import\s*\{[^}]*\}\s*from\s*['"]@vitest\/browser-(?:webdriverio|playwright)['"];?\n?/g, '');

if (needsFactory) {
  const importLine = `import { playwright } from '@vitest/browser-playwright';\n`;
  c = importLine + c;
  c = c.replace(/browser:\s*\{(?:[^{}]|\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})*\}/g,
    `browser: {
        enabled: true,
        name: 'chromium',
        provider: playwright({ launch: { executablePath: '/usr/bin/chromium', args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'] } }),
        headless: true,
        screenshotFailures: false
      }`
  );
} else {
  c = c.replace(/browser:\s*\{(?:[^{}]|\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\})*\}/g,
    `browser: {
        enabled: true,
        name: 'chromium',
        provider: 'playwright',
        headless: true,
        screenshotFailures: false,
        providerOptions: {
          launch: {
            executablePath: '/usr/bin/chromium',
            args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu']
          }
        }
      }`
  );
}

const flakyExcludes = [
  './test/unit/visual/**',
  './test/unit/io/files.js',
  './test/unit/io/loadModel.js',
  './test/unit/io/saveTable.js',
  './test/unit/accessibility/outputs.js',
  './test/unit/core/environment.js',
  './test/unit/type/loading.js',
  './test/unit/type/attributes.js',
  './test/unit/type/p5.Font.js',
  './test/unit/webgl/p5.Framebuffer.js',
  './test/unit/webgl/p5.Camera.js',
  './test/unit/webgl/p5.Texture.js',
  './test/unit/image/downloading.js',
  './test/unit/webgl/p5.Shader.js'
];
const excludeStr = flakyExcludes.map(e => `'${e}'`).join(',\n        ');
c = c.replace(/(exclude:\s*\[)([^\]]*?)(\])/gs, (match, open, items, close) => {
  const existing = items.trim();
  const sep = existing.endsWith(',') || !existing ? '' : ',';
  return `${open}${items}${sep}\n        ${excludeStr}\n      ${close}`;
});

fs.writeFileSync(f, c);
""",
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

npm install
npm install playwright 2>/dev/null || true
# Install @vitest/browser-playwright at same version as @vitest/browser to avoid version mismatch
VITEST_BROWSER_VER=$(node -e "try{{const p=require('@vitest/browser/package.json');console.log(p.version)}}catch(e){{console.log('')}}")
if [ -n "$VITEST_BROWSER_VER" ]; then
  npm install --legacy-peer-deps "@vitest/browser-playwright@$VITEST_BROWSER_VER" 2>/dev/null || true
fi
npx playwright install chromium 2>/dev/null || true

node /home/patch_vitest.js
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
PROJECT=unit
if grep -q "name: 'unit-tests'" vitest.workspace.mjs 2>/dev/null || grep -q "name: 'unit-tests'" vitest.config.js 2>/dev/null; then
  PROJECT=unit-tests
fi
TARGET_TESTS="{t}"
timeout 1200 ./node_modules/.bin/vitest run --project=$PROJECT $TARGET_TESTS --reporter=verbose || true

""".format(pr=self.pr, t=targets),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch || true
npm install
npm install playwright 2>/dev/null || true
VITEST_BROWSER_VER=$(node -e "try{{const p=require('@vitest/browser/package.json');console.log(p.version)}}catch(e){{console.log('')}}")
if [ -n "$VITEST_BROWSER_VER" ]; then
  npm install --legacy-peer-deps "@vitest/browser-playwright@$VITEST_BROWSER_VER" 2>/dev/null || true
fi
node /home/patch_vitest.js
PROJECT=unit
if grep -q "name: 'unit-tests'" vitest.workspace.mjs 2>/dev/null || grep -q "name: 'unit-tests'" vitest.config.js 2>/dev/null; then
  PROJECT=unit-tests
fi
TARGET_TESTS="{t}"
timeout 1200 ./node_modules/.bin/vitest run --project=$PROJECT $TARGET_TESTS --reporter=verbose || true

""".format(pr=self.pr, t=targets),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}
git apply --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.mp4' --exclude='*.ttf' --exclude='*.otf' --exclude='*.woff' --exclude='*.stl' --exclude='*.webm' --exclude='*.wav' --exclude='*.mp3' --exclude='package-lock.json' --whitespace=nowarn /home/test.patch /home/fix.patch || true
npm install
npm install playwright 2>/dev/null || true
VITEST_BROWSER_VER=$(node -e "try{{const p=require('@vitest/browser/package.json');console.log(p.version)}}catch(e){{console.log('')}}")
if [ -n "$VITEST_BROWSER_VER" ]; then
  npm install --legacy-peer-deps "@vitest/browser-playwright@$VITEST_BROWSER_VER" 2>/dev/null || true
fi
node /home/patch_vitest.js
PROJECT=unit
if grep -q "name: 'unit-tests'" vitest.workspace.mjs 2>/dev/null || grep -q "name: 'unit-tests'" vitest.config.js 2>/dev/null; then
  PROJECT=unit-tests
fi
TARGET_TESTS="{t}"
timeout 1200 ./node_modules/.bin/vitest run --project=$PROJECT $TARGET_TESTS --reporter=verbose || true

""".format(pr=self.pr, t=targets),
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


class P5JS_8476_to_7653(Instance):
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
            line = ansi_escape.sub("", line)

            # vitest 2.x verbose format: " ✓ |unit| path > suite > test name (5ms)"
            # vitest 4.x verbose format: " ✓  unit-tests (chromium)  file > suite > test name 5ms"
            # Also handle failure markers
            match = re.match(
                r"^\s*([✓✔×])\s+(?:\|[^|]+\|\s+)?(.+?)(?:\s+\d+ms)?\s*$", line
            )
            if not match:
                # Also try matching failure lines like "× |unit| path..."
                continue

            status, test_name = match.groups()
            test_name = test_name.strip()

            if status in ("✓", "✔"):
                passed_tests.add(test_name)
            elif status == "×":
                failed_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
