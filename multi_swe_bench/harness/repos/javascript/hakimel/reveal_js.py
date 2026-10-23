import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NODE_IMAGE = "node:20-bookworm"

_BASE_TAG = "base-qunit"

_PUPPETEER_CORE_VERSION = "23.11.1"

_APT_PACKAGES = [
    "ca-certificates",
    "curl",
    "git",
    "build-essential",
    "python3",
    "chromium",
    "fonts-liberation",
]

_PATCH_EXCLUDES = " ".join(
    f"--exclude='*{ext}'"
    for ext in [
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".svgz",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".webm",
        ".ogg",
        ".pdf",
        ".zip",
        ".gz",
    ]
)

_ENV = """\
export CI=true
export NODE_ENV=test
export npm_config_update_notifier=false
export npm_config_fund=false
export npm_config_audit=false
export npm_config_loglevel=error
export NODE_OPTIONS=--max-old-space-size=4096
export NODE_PATH=/usr/local/lib/node_modules
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
export CHROME_BIN=/usr/bin/chromium
export QUNIT_PORT=8009
export QUNIT_TIMEOUT=120000
"""

_QUNIT_RUNNER = """\
const fs = require("fs");
const http = require("http");
const path = require("path");
const puppeteer = require("puppeteer-core");

const root = process.cwd();
const port = Number(process.env.QUNIT_PORT || 8009);
const budget = Number(process.env.QUNIT_TIMEOUT || 120000);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".htm": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".cjs": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".map": "application/json; charset=utf-8",
};

const server = http.createServer((req, res) => {
  const raw = String(req.url || "/").split("?")[0].split("#")[0];
  let decoded = raw;
  try {
    decoded = decodeURIComponent(raw);
  } catch (err) {
    decoded = raw;
  }
  const target = path.resolve(root, "." + path.posix.normalize(decoded));
  if (target !== root && !target.startsWith(root + path.sep)) {
    res.statusCode = 403;
    res.end("forbidden");
    return;
  }
  fs.stat(target, (err, stat) => {
    if (err || !stat.isFile()) {
      res.statusCode = 404;
      res.end("not found");
      return;
    }
    res.setHeader("Content-Type", MIME[path.extname(target).toLowerCase()] || "application/octet-stream");
    res.setHeader("Cache-Control", "no-store");
    fs.createReadStream(target).pipe(res);
  });
});

function installHook() {
  window.__QUNIT_RESULTS__ = [];
  window.__QUNIT_DONE__ = false;
  let instance;
  const attach = (q) => {
    if (!q || q.__QUNIT_HOOKED__) {
      return;
    }
    q.__QUNIT_HOOKED__ = true;
    try {
      q.config.autostart = q.config.autostart;
    } catch (err) {
      void err;
    }
    try {
      q.testDone((details) => {
        window.__QUNIT_RESULTS__.push({
          module: String(details.module === undefined || details.module === null ? "" : details.module),
          name: String(details.name === undefined || details.name === null ? "" : details.name),
          failed: Number(details.failed || 0),
          total: Number(details.total || 0),
          skipped: Boolean(details.skipped),
          todo: Boolean(details.todo),
        });
      });
      q.done(() => {
        window.__QUNIT_DONE__ = true;
      });
    } catch (err) {
      window.__QUNIT_HOOK_ERROR__ = String(err && err.message ? err.message : err);
    }
  };
  try {
    Object.defineProperty(window, "QUnit", {
      configurable: true,
      get() {
        return instance;
      },
      set(value) {
        instance = value;
        attach(value);
      },
    });
  } catch (err) {
    void err;
  }
  const poll = setInterval(() => {
    if (window.QUnit) {
      attach(window.QUnit);
    }
    if (window.__QUNIT_DONE__) {
      clearInterval(poll);
    }
  }, 10);
}

function discoverPages() {
  const dir = path.join(root, "test");
  if (!fs.existsSync(dir)) {
    return [];
  }
  return fs
    .readdirSync(dir)
    .filter((name) => name.toLowerCase().endsWith(".html"))
    .sort()
    .map((name) => "test/" + name);
}

function identity(page, result) {
  return [page, result.module, result.name]
    .filter((part) => String(part).length > 0)
    .join(" > ")
    .replace(/\\s+/g, " ")
    .trim();
}

async function main() {
  const pages = discoverPages();
  if (pages.length === 0) {
    console.log("QUNIT_NO_TEST_PAGES");
    process.exitCode = 1;
    return;
  }

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });

  const browser = await puppeteer.launch({
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium",
    headless: true,
    args: [
      "--no-sandbox",
      "--disable-setuid-sandbox",
      "--disable-dev-shm-usage",
      "--disable-gpu",
      "--disable-extensions",
      "--no-first-run",
      "--allow-file-access-from-files",
      "--autoplay-policy=no-user-gesture-required",
    ],
  });

  let problems = 0;

  for (const page of pages) {
    const tab = await browser.newPage();
    tab.setDefaultTimeout(budget);
    tab.on("pageerror", (err) => {
      console.log("QUNIT_PAGE_EXCEPTION " + page + " :: " + String(err && err.message ? err.message : err).split("\\n")[0]);
    });
    await tab.evaluateOnNewDocument(installHook);

    let reached = true;
    try {
      await tab.goto("http://127.0.0.1:" + port + "/" + page, { waitUntil: "load", timeout: budget });
      await tab.waitForFunction("window.__QUNIT_DONE__ === true", { timeout: budget, polling: 100 });
    } catch (err) {
      reached = false;
      console.log("QUNIT_PAGE_INCOMPLETE " + page + " :: " + String(err && err.message ? err.message : err).split("\\n")[0]);
    }

    let results = [];
    try {
      results = await tab.evaluate("window.__QUNIT_RESULTS__ || []");
    } catch (err) {
      console.log("QUNIT_PAGE_UNREADABLE " + page + " :: " + String(err && err.message ? err.message : err).split("\\n")[0]);
    }

    for (const result of results) {
      let status = "PASSED";
      if (result.todo) {
        status = "SKIPPED";
      } else if (result.failed > 0) {
        status = "FAILED";
        problems += 1;
      } else if (result.skipped) {
        status = "SKIPPED";
      }
      console.log("QUNIT_RESULT " + status + " " + identity(page, result));
    }

    if (results.length === 0) {
      console.log("QUNIT_NO_RESULTS " + page);
      problems += 1;
    } else if (!reached) {
      problems += 1;
    }

    await tab.close();
  }

  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  process.exitCode = problems > 0 ? 1 : 0;
}

main().catch((err) => {
  console.log("QUNIT_RUNNER_CRASH :: " + String(err && err.stack ? err.stack : err));
  process.exitCode = 1;
});
"""

_CHECK_GIT_CHANGES = """\
#!/bin/bash
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

_PREPARE = """\
#!/bin/bash
set -euo pipefail

__ENV__
cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

node --version
npm --version

npm install --legacy-peer-deps --ignore-scripts

for lockfile in package-lock.json npm-shrinkwrap.json; do
  if git ls-files --error-unmatch "$lockfile" > /dev/null 2>&1; then
    git checkout -- "$lockfile"
  fi
done

test -f /home/qunit_runner.js
test -x "$PUPPETEER_EXECUTABLE_PATH"
git diff --quiet HEAD -- package-lock.json npm-shrinkwrap.json
node -e "require.resolve('puppeteer-core'); require('puppeteer-core')"

ls test/*.html > /home/test-pages.log
test -s /home/test-pages.log
wc -l < /home/test-pages.log

if [ -f gulpfile.js ] || [ -f gulpfile.babel.js ]; then
  test -x ./node_modules/.bin/gulp
  test -f node_modules/qunit/qunit/qunit.js
fi

node -e "require('./package.json'); console.log('DEPS_OK', process.version)"
"""

_STAGE = """\
#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
__APPLY__
if ! git diff --quiet HEAD -- package.json; then
  npm install --legacy-peer-deps --ignore-scripts
fi

if [ -f gulpfile.js ] || [ -f gulpfile.babel.js ]; then
  AVAILABLE=$(npx gulp --tasks-simple 2>/dev/null | tr '\\n' ' ') || AVAILABLE=""
  BUILD_TASKS=""
  for task in css js plugins; do
    case " $AVAILABLE " in
      *" $task "*) BUILD_TASKS="$BUILD_TASKS $task" ;;
    esac
  done
  if [ -n "$BUILD_TASKS" ]; then
    npx gulp $BUILD_TASKS
  fi
fi

STATUS=0
node /home/qunit_runner.js || STATUS=$?
exit $STATUS
"""

_PRUNE = """\
RUN set -eux; \\
    cd /home/__REPO__; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        cd /home/__REPO__ && git submodule foreach --recursive ' \\
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
"""


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__ENV__", _ENV)
        .replace("__EXCLUDES__", _PATCH_EXCLUDES)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__REPO__", pr.repo)
    )


class RevealJsImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        packages = " \\\n        ".join(_APT_PACKAGES)
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {_NODE_IMAGE}",
            DockerfileEnhancer._infrastructure_block(self, _NODE_IMAGE).rstrip("\n"),
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"        {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        sections.append(
            "ENV PUPPETEER_SKIP_DOWNLOAD=true \\\n"
            "    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \\\n"
            "    CHROME_BIN=/usr/bin/chromium \\\n"
            "    NODE_PATH=/usr/local/lib/node_modules"
        )
        sections.append(
            f"RUN npm install -g --ignore-scripts puppeteer-core@{_PUPPETEER_CORE_VERSION} \\\n"
            '    && node -e "require(\'puppeteer-core\')" \\\n'
            '    && "$PUPPETEER_EXECUTABLE_PATH" --version'
        )
        sections.append("RUN git config --global --add safe.directory '*'")
        sections.append("WORKDIR /home/")
        sections.append(
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} \\\n'
            f"    && cd /home/{self.pr.repo} \\\n"
            "    && git rev-parse HEAD > /dev/null"
        )
        sections.append(f"WORKDIR /home/{self.pr.repo}")

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class RevealJsImageDefault(Image):
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
        return RevealJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "qunit_runner.js", _QUNIT_RUNNER),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_STAGE, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    f"git apply --whitespace=nowarn {_PATCH_EXCLUDES} /home/test.patch\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    f"git apply --whitespace=nowarn {_PATCH_EXCLUDES} /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        sections = [
            f"FROM {base_name}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\nENV BASE_COMMIT=${{BASE_COMMIT}}',
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(copy_commands)
        sections.append("RUN bash /home/prepare.sh")
        sections.append(_render(_PRUNE, self.pr).rstrip("\n"))

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_RESULT_RE = re.compile(r"^QUNIT_RESULT (PASSED|FAILED|SKIPPED) (.+)$")


@Instance.register("hakimel", "reveal.js")
class REVEAL_JS(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> str | Image | None:
        return RevealJsImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw_line in _ANSI_RE.sub("", test_log).split("\n"):
            match = _RESULT_RE.match(raw_line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2).strip()
            if not name:
                continue
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

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
