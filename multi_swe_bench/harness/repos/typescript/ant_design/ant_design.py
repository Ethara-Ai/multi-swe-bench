"""ant-design/ant-design base config — fallback for PRs without number_interval."""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def parse_jest_log(test_log: str) -> TestResult:
    """Shared Jest parse_log for all ant-design era configs.

    Handles both file-level (PASS/FAIL <path>) and test-level (✓/✕ <name>)
    output from Jest verbose mode. Pattern derived from jestjs/jest.py
    (authoritative Jest config) with ANSI stripping and set-based dedup
    from remotion (gold standard infrastructure).
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI escape codes
    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    # File-level patterns (PASS/FAIL <filepath>)
    # Use \S+ because file paths never contain spaces, avoiding lazy/optional timing ambiguity
    re_pass_file = re.compile(r"^PASS:?\s+(\S+)")
    re_fail_file = re.compile(r"^FAIL:?\s+(\S+)")

    # Test-level patterns (✓/✕ <test name>)
    re_pass_check = re.compile(r"^\s*[✓✔]\s+(.+)$")
    re_pass_timing = re.compile(r"[✓✔]\s+(.+?)\s+\(\d+\s*ms\)")
    re_fail_cross = re.compile(r"^\s*[×✗✕]\s+(.+)$")
    re_fail_timing = re.compile(r"[×✗✕]\s+(.+?)\s+\(\d+\s*ms\)")

    # Skip patterns
    re_skip_circle = re.compile(r"^\s*[○◌]\s+(.+)$")
    re_skip_keyword = re.compile(r"SKIP:?\s+(.+?)\s")

    for line in clean_log.splitlines():
        # Pass — file-level
        m = re_pass_file.match(line)
        if m and m.group(1) not in failed_tests:
            passed_tests.add(m.group(1))
            continue

        # Pass — test-level (timing first — strips timing suffix)
        m = re_pass_timing.search(line)
        if m and m.group(1) not in failed_tests:
            passed_tests.add(m.group(1))
            continue

        # Pass — test-level (checkmark fallback — no timing)
        m = re_pass_check.match(line)
        if m and m.group(1) not in failed_tests:
            passed_tests.add(m.group(1))
            continue

        # Fail — file-level
        m = re_fail_file.match(line)
        if m:
            failed_tests.add(m.group(1))
            passed_tests.discard(m.group(1))
            continue

        # Fail — test-level (timing first — strips timing suffix)
        m = re_fail_timing.search(line)
        if m:
            failed_tests.add(m.group(1))
            passed_tests.discard(m.group(1))
            continue

        # Fail — test-level (cross fallback — no timing)
        m = re_fail_cross.match(line)
        if m:
            failed_tests.add(m.group(1))
            passed_tests.discard(m.group(1))
            continue

        # Skip — circle symbol
        m = re_skip_circle.match(line)
        if m:
            skipped_tests.add(m.group(1))
            continue

        # Skip — keyword
        m = re_skip_keyword.match(line)
        if m:
            skipped_tests.add(m.group(1))
            continue

    # Final dedup: worst wins — sets must be mutually exclusive
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


class AntDesignImageBase(Image):
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
        return "node:18"

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
ENV DEBIAN_FRONTEND=noninteractive

{code}

{self.clear_env}

"""


class AntDesignImageDefault(Image):
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
        return AntDesignImageBase(self.pr, self.config)

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

npm install || true

# Pin transitive deps to prevent version drift (no lockfile in repo)
npm install --no-save cheerio@1.0.0-rc.10 2>/dev/null || true
npm run version || true

# Patch .jest.js to add missing ESM module transforms
node -e "
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {{
    if (!c.includes(\"'\" + m + \"'\")) {{
      c = c.replace('const compileModules = [', \"const compileModules = [\\n  '\" + m + \"',\");
      changed = true;
    }}
  }}
  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }}
}} catch(e) {{ console.log('No .jest.js to patch'); }}
" || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
npx jest --config .jest.js --no-cache --verbose || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
npx jest --config .jest.js --no-cache --verbose || true
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx jest --config .jest.js --no-cache --verbose || true
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault dependency must be an Image")
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


@Instance.register("ant-design", "ant-design")
class AntDesign(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AntDesignImageDefault(self.pr, self._config)

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
        return parse_jest_log(test_log)
