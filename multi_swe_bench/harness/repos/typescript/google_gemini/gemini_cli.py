import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
 

class GeminiCliImageBase(Image):
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

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        # Ensure pnpm/yarn are available when the repo uses them.
        return "RUN corepack enable"


class GeminiCliImageDefault(Image):
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
        return GeminiCliImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

corepack enable || true

install_deps() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm install --frozen-lockfile || pnpm install
  elif [[ -f yarn.lock ]]; then
    yarn install --frozen-lockfile || yarn install
  elif [[ -f package-lock.json ]]; then
    npm ci || npm install
  else
    npm install
  fi
}}

install_deps || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

corepack enable || true

install_deps() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm install --frozen-lockfile || pnpm install
  elif [[ -f yarn.lock ]]; then
    yarn install --frozen-lockfile || yarn install
  elif [[ -f package-lock.json ]]; then
    npm ci || npm install
  else
    npm install
  fi
}}

run_tests() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm test
  elif [[ -f yarn.lock ]]; then
    yarn test
  else
    npm test || npm run test
  fi
}}

install_deps || true

# Build if repo defines it.
if [[ -f pnpm-lock.yaml ]]; then
  pnpm run -s build --if-present || true
elif [[ -f yarn.lock ]]; then
  yarn -s build || true
else
  npm run build --if-present || true
fi

# Run the project's test suite.
run_tests
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

corepack enable || true

install_deps() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm install --frozen-lockfile || pnpm install
  elif [[ -f yarn.lock ]]; then
    yarn install --frozen-lockfile || yarn install
  elif [[ -f package-lock.json ]]; then
    npm ci || npm install
  else
    npm install
  fi
}}

run_tests() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm test
  elif [[ -f yarn.lock ]]; then
    yarn test
  else
    npm test || npm run test
  fi
}}

install_deps || true

if [[ -f pnpm-lock.yaml ]]; then
  pnpm run -s build --if-present || true
elif [[ -f yarn.lock ]]; then
  yarn -s build || true
else
  npm run build --if-present || true
fi

run_tests
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

corepack enable || true

install_deps() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm install --frozen-lockfile || pnpm install
  elif [[ -f yarn.lock ]]; then
    yarn install --frozen-lockfile || yarn install
  elif [[ -f package-lock.json ]]; then
    npm ci || npm install
  else
    npm install
  fi
}}

run_tests() {{
  if [[ -f pnpm-lock.yaml ]]; then
    pnpm test
  elif [[ -f yarn.lock ]]; then
    yarn test
  else
    npm test || npm run test
  fi
}}

install_deps || true

if [[ -f pnpm-lock.yaml ]]; then
  pnpm run -s build --if-present || true
elif [[ -f yarn.lock ]]; then
  yarn -s build || true
else
  npm run build --if-present || true
fi

run_tests
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

{self.clear_env}

"""


@Instance.register("google-gemini", "gemini-cli")
class GeminiCli(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GeminiCliImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^\s*PASS\s+(.*)$"),
            re.compile(r"^\s*[✔✓]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
        ]
        failed_res = [
            re.compile(r"^\s*FAIL\s+(.*)$"),
            re.compile(r"^\s*[×✗✘]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
        ]

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            for pattern in passed_res:
                m = pattern.match(line)
                if m:
                    passed_tests.add(m.group(1).strip())
                    break
            else:
                for pattern in failed_res:
                    m = pattern.match(line)
                    if m:
                        failed_tests.add(m.group(1).strip())
                        break

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

