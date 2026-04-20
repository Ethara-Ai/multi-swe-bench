import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SvelteImageBase8(Image):
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
        return "ubuntu:latest"

    def image_tag(self) -> str:
        return "base8"

    def workdir(self) -> str:
        return "base8"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt update && apt install -y git nodejs npm && npm install -g pnpm@8

{code}

CMD ["pnpm", "playwright", "install", "chromium"]

{self.clear_env}

"""


class SvelteImageBase9(Image):
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
        return "ubuntu:latest"

    def image_tag(self) -> str:
        return "base9"

    def workdir(self) -> str:
        return "base9"

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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt update && apt install -y git nodejs npm && npm install -g pnpm@9

{code}

CMD ["pnpm", "playwright", "install", "chromium"]

{self.clear_env}

"""


def _test_script(repo: str, patches: list[str] | None = None) -> str:
    """Generate a test runner script that detects package manager from lockfile."""
    patch_cmds = ""
    if patches:
        patch_files = " ".join(patches)
        patch_cmds = f"git apply --whitespace=nowarn {patch_files}\n"

    return f"""#!/bin/bash
set -e

cd /home/{repo}
{patch_cmds}
if [ -f pnpm-lock.yaml ]; then
  pnpm test -- --reporter verbose
elif [ -f package-lock.json ]; then
  npm test -- --verbose
elif [ -f yarn.lock ]; then
  npm test -- --verbose
else
  npm test -- --verbose
fi

"""


class SvelteImageDefault(Image):
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
        if self.pr.number <= 11804:
            return SvelteImageBase8(self.pr, self.config)
        else:
            return SvelteImageBase9(self.pr, self.config)

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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

if [ -f pnpm-lock.yaml ]; then
  pnpm install --frozen-lockfile || true
elif [ -f package-lock.json ]; then
  npm install
elif [ -f yarn.lock ]; then
  npm install
else
  npm install
fi

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                _test_script(self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                _test_script(self.pr.repo, patches=["/home/test.patch"]),
            ),
            File(
                ".",
                "fix-run.sh",
                _test_script(self.pr.repo, patches=["/home/test.patch", "/home/fix.patch"]),
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


@Instance.register("sveltejs", "svelte")
class Svelte(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SvelteImageDefault(self.pr, self._config)

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

        # Vitest patterns (used by newer Svelte PRs)
        re_vitest_pass = re.compile(r"^✓ (.+?)(?:\s*\d*\.?\d+\s*(?:ms|s|m))?$")
        re_vitest_fail = re.compile(r"^× (.+?)(?:\s*\d*\.?\d+\s*(?:ms|s|m))?$")

        # Mocha patterns (used by older Svelte PRs)
        # Suite headers: lines with NO leading marker (✓, ×, -, N)) that are not blank
        # Test markers: ✓ (pass), - (skip), N) (inline fail marker in suite listing)
        # "N passing", "N pending", "N failing" are summary lines
        re_mocha_summary = re.compile(r"^\d+ (?:passing|pending|failing)")
        re_mocha_inline_fail = re.compile(r"^\d+\)\s+(.+)")
        # Error details section: "  N) suite-name\n       test-name:\n     Error: ..."
        re_error_number = re.compile(r"^\d+\)\s+(.+)")

        # Detect mocha vs vitest by scanning for mocha summary lines
        lines = test_log.splitlines()
        is_mocha = any(re_mocha_summary.match(l.strip()) for l in lines)

        if not is_mocha:
            # Vitest mode: simple pass/fail, no suite disambiguation needed
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("✓"):
                    match = re_vitest_pass.match(line)
                    if match:
                        passed_tests.add(match.group(1))
                    else:
                        passed_tests.add(line.replace("✓", "").strip())
                    continue
                fail_match = re_vitest_fail.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
        else:
            # Mocha mode: track current suite to prefix test names
            # Suite detection: a line with lower indent than test lines, no marker
            current_suite = ""
            in_summary = False

            for line in lines:
                stripped = line.strip()
                if not stripped:
                    continue

                # After "N passing/pending/failing" lines, we're in error details
                if re_mocha_summary.match(stripped):
                    in_summary = True
                    continue

                if in_summary:
                    # In error details: parse "N) suite-name" + "test-name:" pairs
                    # Format: "  N) suite-name\n       test-name:"
                    error_match = re_error_number.match(stripped)
                    if error_match:
                        # This is the suite name line like "1) custom-elements"
                        current_suite = error_match.group(1)
                        continue
                    # Test name line ends with ":"
                    if stripped.endswith(":") and current_suite:
                        test_name = stripped.rstrip(":")
                        failed_tests.add(f"{current_suite} > {test_name}")
                        continue
                    continue

                # Regular test output section
                # Detect suite headers: lines that don't start with ✓, -, × or N)
                # and have less indentation than test lines
                raw_indent = len(line) - len(line.lstrip())

                if stripped.startswith("✓"):
                    match = re_vitest_pass.match(stripped)
                    name = match.group(1) if match else stripped.replace("✓", "").strip()
                    qualified = f"{current_suite} > {name}" if current_suite else name
                    passed_tests.add(qualified)
                    continue

                if stripped.startswith("- "):
                    name = stripped[2:].strip()
                    qualified = f"{current_suite} > {name}" if current_suite else name
                    skipped_tests.add(qualified)
                    continue

                if re_mocha_inline_fail.match(stripped):
                    # Inline fail marker like "1) test-name" within suite listing
                    # Don't add to failed here; real failures come from error details
                    continue

                # Likely a suite header if indentation is low (2 spaces typical)
                # and doesn't match test patterns or noise (warnings, build output)
                if raw_indent <= 4 and not stripped.startswith("(") and not stripped.startswith("Error"):
                    # Filter out build output, warnings, etc.
                    if not any(c in stripped for c in [":", "/", "→", "!", ">"]):
                        current_suite = stripped

        # Resolve overlaps: failed > passed > skipped
        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            passed_tests=passed_tests,
            failed_count=len(failed_tests),
            failed_tests=failed_tests,
            skipped_count=len(skipped_tests),
            skipped_tests=skipped_tests,
        )
