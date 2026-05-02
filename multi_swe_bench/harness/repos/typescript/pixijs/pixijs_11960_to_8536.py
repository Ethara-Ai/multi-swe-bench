import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "pixijs"


class PixijsImageBase(Image):
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
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo_dir}".format(
                org=self.pr.org, repo=self.pr.repo, repo_dir=REPO_DIR
            )
        else:
            code = "COPY {repo} /home/{repo_dir}".format(
                repo=self.pr.repo, repo_dir=REPO_DIR
            )

        return """FROM {image_name}

{global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
ENV CI=true
ENV JEST_ELECTRON_NO_SANDBOX=1
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    xvfb \\
    libgtk-3-0 \\
    libnotify4 \\
    libnss3 \\
    libxss1 \\
    libxtst6 \\
    xauth \\
    libgbm1 \\
    libasound2 \\
    libatk-bridge2.0-0 \\
    libdrm2 \\
    libxkbcommon0 \\
    libxcomposite1 \\
    libxdamage1 \\
    libxfixes3 \\
    libxrandr2 \\
    libpango-1.0-0 \\
    libcairo2 \\
    libcups2 \\
    libdbus-1-3 \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g http-server

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class PixijsImageDefault(Image):
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
        return PixijsImageBase(self.pr, self.config)

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

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

# Strip engines field to avoid npm install refusal on Node 20
python3 -c "
import json
with open('package.json') as f:
    d = json.load(f)
d.pop('engines', None)
with open('package.json', 'w') as f:
    json.dump(d, f, indent=2)
"

# Install dependencies — skip native module builds (gl) that aren't needed for jest
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true

# Install all electron binaries (multiple may exist in workspace packages)
find node_modules -path '*/electron/install.js' -exec sh -c 'cd "$(dirname "$0")" && node install.js' {{}} \\; 2>&1 || true

# Build packages if this is a workspace project
if [ -f "lerna.json" ] || grep -q '"workspaces"' package.json 2>/dev/null; then
    npx lerna run build --stream 2>&1 || npm run build 2>&1 || true
fi

""".format(repo_dir=REPO_DIR, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}

# Start Xvfb
Xvfb :99 -screen 0 1024x768x24 &
sleep 1

# Detect test command
if grep -q '"test":.*"node.*test.mts"' package.json 2>/dev/null; then
    node ./scripts/test.mts 2>&1
elif grep -q '"test":.*"run-s' package.json 2>/dev/null; then
    npx run-s test:unit test:scene 2>&1
else
    ./node_modules/.bin/jest --silent 2>&1
fi

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}

# Apply test patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

# Reinstall in case patch adds dependencies
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
find node_modules -path '*/electron/install.js' -exec sh -c 'cd "$(dirname "$0")" && node install.js' {{}} \\; 2>&1 || true

# Rebuild if workspace project
if [ -f "lerna.json" ] || grep -q '"workspaces"' package.json 2>/dev/null; then
    npx lerna run build --stream 2>&1 || npm run build 2>&1 || true
fi

# Start Xvfb
Xvfb :99 -screen 0 1024x768x24 &
sleep 1

# Detect and run tests
if grep -q '"test":.*"node.*test.mts"' package.json 2>/dev/null; then
    node ./scripts/test.mts 2>&1
elif grep -q '"test":.*"run-s' package.json 2>/dev/null; then
    npx run-s test:unit test:scene 2>&1
else
    ./node_modules/.bin/jest --silent 2>&1
fi

""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}

# Apply test patch then fix patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

# Reinstall in case patches add dependencies
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
find node_modules -path '*/electron/install.js' -exec sh -c 'cd "$(dirname "$0")" && node install.js' {{}} \\; 2>&1 || true

# Rebuild if workspace project
if [ -f "lerna.json" ] || grep -q '"workspaces"' package.json 2>/dev/null; then
    npx lerna run build --stream 2>&1 || npm run build 2>&1 || true
fi

# Start Xvfb
Xvfb :99 -screen 0 1024x768x24 &
sleep 1

# Detect and run tests
if grep -q '"test":.*"node.*test.mts"' package.json 2>/dev/null; then
    node ./scripts/test.mts 2>&1
elif grep -q '"test":.*"run-s' package.json 2>/dev/null; then
    npx run-s test:unit test:scene 2>&1
else
    ./node_modules/.bin/jest --silent 2>&1
fi

""".format(repo_dir=REPO_DIR),
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


@Instance.register("pixijs", "pixijs_11960_to_8536")
class Pixijs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PixijsImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Jest individual test patterns
        # ✓ test name (Nms)
        re_jest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        # ✕ test name (Nms)
        re_jest_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        # ○ skipped test name
        re_jest_skip = re.compile(r"^\s*○\s+(.+)$")

        # Jest file-level PASS/FAIL lines
        re_jest_file_pass = re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:s|ms)\))?$")
        re_jest_file_fail = re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:s|ms)\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_jest_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_jest_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_jest_file_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

        # Fail wins over pass
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
