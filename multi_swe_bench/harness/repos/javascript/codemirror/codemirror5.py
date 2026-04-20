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
        return "node:20"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # Build sections list; DockerfileEnhancer injects infra between FROM and rest
        # so avoid blank lines right after FROM (enhancer adds its own separators)
        parts = []

        if self.global_env:
            parts.append(self.global_env)

        parts.append("WORKDIR /home/")

        parts.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    git curl wget xvfb chromium \\\n"
            "    fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg fonts-kacst fonts-freefont-ttf && \\\n"
            "    rm -rf /var/lib/apt/lists/*"
        )

        parts.append(
            "ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true\n"
            "ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium\n"
            "ENV CHROME_BIN=/usr/bin/chromium"
        )

        parts.append(code)

        if self.clear_env:
            parts.append(self.clear_env)

        body = "\n\n".join(parts)
        return f"FROM {image_name}\n{body}\n"


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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Install deps; use --ignore-scripts to avoid phantomjs binary download failure
npm install --ignore-scripts || true

# Install puppeteer (used as replacement for deprecated phantomjs)
npm install puppeteer@21 --ignore-scripts --save-dev || true

# Write custom puppeteer-based test runner to /home/ (NOT into repo tree)
# This avoids conflicts with git apply in test-run.sh / fix-run.sh
cat > /home/custom_test_run.js << 'TESTRUNNER'
#!/usr/bin/env node
var ok = true;
try {{ ok = require("./lint").ok; }} catch(e) {{ ok = true; }}

var files = new (require('node-static').Server)();

var server = require('http').createServer(function (req, res) {{
  req.addListener('end', function () {{
    files.serve(req, res, function (err, result) {{
      if (err) {{
        res.writeHead(err.status, err.headers);
        res.end();
      }}
    }});
  }}).resume();
}}).addListener('error', function (err) {{
  throw err;
}}).listen(3000, async function () {{
  try {{
    var puppeteer = require("puppeteer");
    var browser = await puppeteer.launch({{
      headless: "new",
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || process.env.CHROME_BIN || undefined,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
    }});
    var page = await browser.newPage();
    page.on('console', function(msg) {{ console.log(msg.text()); }});
    // Use #verbose to make index.html output ALL test results (pass/fail/skip) to #output
    await page.goto('http://localhost:3000/test/index.html#verbose', {{ waitUntil: 'domcontentloaded', timeout: 120000 }});
    // Wait for #status to have class containing "done" (old CM5 has no window.done)
    await page.waitForFunction(function() {{
      var s = document.getElementById('status');
      return s && s.className && s.className.indexOf('done') !== -1;
    }}, {{ timeout: 300000 }});
    // Extract output text and status
    var result = await page.evaluate(function() {{
      var output = document.getElementById('output');
      var status = document.getElementById('status');
      // Walk through each <dl> in output to get structured results
      var lines = [];
      if (output) {{
        var dls = output.getElementsByTagName('dl');
        for (var i = 0; i < dls.length; i++) {{
          var dd = dls[i].getElementsByTagName('dd')[0];
          if (dd) lines.push(dd.textContent);
        }}
      }}
      var statusText = status ? status.textContent : '';
      var failed = (status && status.className.indexOf('fail') !== -1);
      return {{ output: lines.join('\\n'), status: statusText, failed: failed }};
    }});
    console.log(result.output);
    console.log(result.status);
    await browser.close();
    server.close();
    process.exit(result.failed ? 1 : 0);
  }} catch(e) {{
    console.error(e);
    server.close();
    process.exit(1);
  }}
}});
TESTRUNNER

npm run build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Copy custom puppeteer test runner into place (replaces phantomjs-based one)
cp /home/custom_test_run.js test/run.js
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
sleep 2
npm test 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Apply test patch on clean git state (before custom runner replaces test/run.js)
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
# Now copy custom puppeteer test runner (overrides whatever test/run.js the patch set)
cp /home/custom_test_run.js test/run.js
# Reinstall deps in case test patch changed package.json
npm install --ignore-scripts || true
npm install puppeteer@21 --ignore-scripts --save-dev || true
npm run build || true
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
sleep 2
npm test 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
# Apply test+fix patches on clean git state (before custom runner replaces test/run.js)
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
# Now copy custom puppeteer test runner (overrides whatever test/run.js the patch set)
cp /home/custom_test_run.js test/run.js
# Reinstall deps in case patches changed package.json
npm install --ignore-scripts || true
npm install puppeteer@21 --ignore-scripts --save-dev || true
npm run build || true
Xvfb :99 -screen 0 1024x768x24 &
export DISPLAY=:99
sleep 2
npm test 2>&1 || true

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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("codemirror", "codemirror5")
class CodeMirror5(Instance):
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

        re_pass_test = re.compile(r"^Test '([^'\"]+)' (?:succeeded|passed)")
        re_fail_test = re.compile(r"^Test '([^'\"]+)' failed")
        re_skip_test = re.compile(r"^Test '([^'\"]+)' skipped")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_test.search(line)
            if pass_match:
                passed_tests.add(pass_match.group(1))

            fail_match = re_fail_test.search(line)
            if fail_match:
                failed_tests.add(fail_match.group(1))

            skip_match = re_skip_test.search(line)
            if skip_match:
                skipped_tests.add(skip_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
