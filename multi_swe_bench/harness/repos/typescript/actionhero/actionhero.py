"""actionhero/actionhero harness config — Jest + ts-jest, npm, Redis dependency."""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ActionheroImageBase(Image):
    """Base Docker image: node:18 with Redis server.

    actionhero is a Node.js API framework.  Tests are run via Jest with
    ts-jest for TypeScript support.  A local Redis instance is required
    at runtime (the framework reads REDIS_HOST / REDIS_PORT from env).

    Note: node:16 is based on Debian Buster (EOL — apt repos return 404),
    so we use node:18 (Debian Bookworm) which has active package repos.
    """

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
        return "node:16-bullseye"

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
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    redis-server \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ActionheroImageDefault(Image):
    """PR-specific Docker layer: patches, prepare, and run scripts.

    actionhero test pipeline:

        git checkout <base_sha>
        npm install
        redis-server --daemonize yes
        npx jest --ci --verbose
    """

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
        return ActionheroImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

PUPPETEER_SKIP_DOWNLOAD=true npm install --legacy-peer-deps
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Start Redis (required by actionhero)
redis-server --daemonize yes
sleep 1

npx jest --ci --verbose 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply /home/test.patch

PUPPETEER_SKIP_DOWNLOAD=true npm install --legacy-peer-deps

# Start Redis (required by actionhero)
redis-server --daemonize yes
sleep 1

npx jest --ci --verbose 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply /home/test.patch /home/fix.patch

PUPPETEER_SKIP_DOWNLOAD=true npm install --legacy-peer-deps

# Start Redis (required by actionhero)
redis-server --daemonize yes
sleep 1

npx jest --ci --verbose 2>&1
""".format(repo=self.pr.repo),
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


@Instance.register("actionhero", "actionhero")
class Actionhero(Instance):
    """Harness instance for actionhero/actionhero — Jest + ts-jest, Redis."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ActionheroImageDefault(self.pr, self._config)

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
        """Parse Jest verbose output into pass/fail/skip sets.

        Jest verbose output looks like:

            PASS __tests__/core/api.ts
              api
                ✓ can retrieve server uptime via the api (45 ms)
                ✕ should fail on unknown action (12 ms)
                ○ skipped - should handle edge case

            FAIL __tests__/core/cache.ts
              cache
                ✓ can save and load data (31 ms)
                ✕ should handle expiration (5 ms)

        Suite-level PASS/FAIL lines indicate the overall file status.
        Individual test markers: ✓/✔ = pass, ×/✗/✕ = fail, ○/◌ = skip.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Jest output patterns
        # Suite level: "PASS tests/file.test.ts" or "FAIL tests/file.test.ts"
        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?s\))?$"),
            re.compile(r"^\s*[✓✔]\s+(.+)$"),
        ]

        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?s\))?$"),
            re.compile(r"^\s*[×✗✕]\s+(.+)$"),
        ]

        skipped_res = [
            re.compile(r"^\s*[○◌]\s+(.+)$"),
        ]

        # Strip Jest timing metadata from test names: "(123 ms)" or "(1.5 s)"
        timing_re = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m:
                    test_name = timing_re.sub("", m.group(1).strip())
                    if test_name not in failed_tests:
                        passed_tests.add(test_name)

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    test_name = timing_re.sub("", m.group(1).strip())
                    failed_tests.add(test_name)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    test_name = timing_re.sub("", m.group(1).strip())
                    if test_name not in passed_tests and test_name not in failed_tests:
                        skipped_tests.add(test_name)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
