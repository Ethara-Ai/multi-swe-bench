import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


REPO_DIR = "slidev"


class SlidevImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack disable 2>/dev/null || true
RUN npm install -g pnpm@10.33.2

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


class SlidevImageDefault(Image):
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
        return SlidevImageBase(self.pr, self.config)

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

# Strip packageManager field so global pnpm 10 is used
python3 -c "
import json
with open('package.json') as f:
    d = json.load(f)
d.pop('packageManager', None)
# Allow esbuild postinstall to run (pnpm 10 security policy)
d.setdefault('pnpm', {{}})['onlyBuiltDependencies'] = ['esbuild']
with open('package.json', 'w') as f:
    json.dump(d, f, indent=2)
"

pnpm install --no-frozen-lockfile 2>&1 || true

# Build workspace packages (vitest-era PRs need compiled deps)
pnpm -r --filter=./packages/** run build 2>&1 || true

""".format(repo_dir=REPO_DIR, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo_dir}
pnpm test 2>&1

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

# Remove lockfile to avoid patch conflicts — pnpm install regenerates it
rm -f pnpm-lock.yaml

# Reinstall in case patch adds/changes dependencies
pnpm install --no-frozen-lockfile 2>&1 || true

# Rebuild workspace packages
pnpm -r --filter=./packages/** run build 2>&1 || true

pnpm test 2>&1

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

# Remove lockfile to avoid patch conflicts — pnpm install regenerates it
rm -f pnpm-lock.yaml

# Reinstall in case patches add/change dependencies
pnpm install --no-frozen-lockfile 2>&1 || true

# Rebuild workspace packages
pnpm -r --filter=./packages/** run build 2>&1 || true

pnpm test 2>&1

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


@Instance.register("slidevjs", "slidev_2466_to_3")
class Slidev(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SlidevImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Vitest output patterns (strips timing and test count metadata for cross-stage consistency)
        re_vitest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\(\d+\s*tests?\))?(?:\s+\d+ms)?$")
        re_vitest_fail = re.compile(r"^\s*[❯×✗]\s+(.+?)(?:\s+\(\d+\s*tests?\))?(?:\s+\d+ms)?$")
        re_vitest_skip = re.compile(r"^\s*-\s+(.+?)(?:\s+\(\d+\s*tests?\))?(?:\s+\d+ms)?$")

        # Jest output patterns (individual tests)
        re_jest_pass = re.compile(r"^\s*[✓✔√]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        re_jest_skip = re.compile(r"^\s*○\s+(.+)$")

        # Jest file-level patterns (PASS/FAIL test/file.ts)
        re_jest_file_pass = re.compile(r"^PASS\s+(.+)$")
        re_jest_file_fail = re.compile(r"^FAIL\s+(.+)$")

        # Vitest summary line: Tests  4 passed (4)
        re_vitest_summary = re.compile(
            r"^\s*Tests?\s+(\d+)\s+passed\s*(?:\|\s*(\d+)\s+failed)?"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            # Vitest patterns
            match = re_vitest_pass.match(line)
            if match:
                passed_tests.add(match.group(1).strip())
                continue

            match = re_vitest_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_vitest_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            # Jest patterns
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

        # Remove any test that shows up in both pass and fail (fail wins)
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
