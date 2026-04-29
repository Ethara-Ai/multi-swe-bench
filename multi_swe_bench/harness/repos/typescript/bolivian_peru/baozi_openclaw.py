import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BaoziOpenclawImageBase(Image):
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
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt update && apt install -y python3
RUN npm install -g tsx
{code}

{self.clear_env}

"""


class BaoziOpenclawImageDefault(Image):
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
        return BaoziOpenclawImageBase(self.pr, self._config)

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

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                r"""#!/bin/bash
cd /home/{pr.repo}

# Find all sub-project directories that have a package.json
# Each PR creates its own sub-project from scratch on an empty base
for proj_dir in $(find . -name package.json -not -path '*/node_modules/*' -exec dirname {{}} \;); do
    echo "=== Entering sub-project: $proj_dir ==="
    cd "/home/{pr.repo}/$proj_dir"

    # Install dependencies
    npm install 2>&1 || true

    # Detect and run the appropriate test runner
    # 1. Check package.json scripts.test for jest
    if node -e "const p=require('./package.json'); process.exit(p.scripts && p.scripts.test && p.scripts.test.includes('jest') ? 0 : 1)" 2>/dev/null; then
        echo "=== Detected Jest ==="
        node --experimental-vm-modules node_modules/.bin/jest --verbose --no-coverage --transform='{{"^.+\\.tsx?$":"ts-jest"}}' || true

    # 2. Check package.json scripts.test for tsx src/tests/run.ts (PR 68 custom runner)
    elif node -e "const p=require('./package.json'); process.exit(p.scripts && p.scripts.test && p.scripts.test.includes('tsx') ? 0 : 1)" 2>/dev/null; then
        echo "=== Detected custom tsx runner ==="
        npx tsx $(node -e "const p=require('./package.json'); console.log(p.scripts.test.replace(/^tsx\s+/, '').replace(/^npx tsx\s+/, ''))") || true

    # 3. Check for vitest config or vitest imports in test files
    elif [ -f "vitest.config.ts" ] || [ -f "vitest.config.js" ] || [ -f "vitest.config.mts" ] || grep -rq "from 'vitest'" test/ 2>/dev/null || grep -rq 'from "vitest"' test/ 2>/dev/null; then
        echo "=== Detected Vitest ==="
        npx vitest run --reporter=verbose || true

    # 4. Check for node:test imports in test files (TAP output)
    elif grep -rq "from 'node:test'" test/ 2>/dev/null || grep -rq 'from "node:test"' test/ 2>/dev/null; then
        echo "=== Detected node:test (TAP) ==="
        for tf in $(find test/ -name '*.test.ts' -o -name '*.test.js' 2>/dev/null); do
            npx tsx "$tf" || true
        done

    # 5. Fallback: run tsx on test files directly
    elif [ -d "test" ]; then
        echo "=== Fallback: tsx test runner ==="
        for tf in $(find test/ -name '*.test.ts' -o -name '*.test.js' 2>/dev/null); do
            npx tsx "$tf" || true
        done
    fi

    cd "/home/{pr.repo}"
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch

bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --reject /home/fix.patch || true

bash /home/run_tests.sh

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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("bolivian-peru", "baozi-openclaw")
class BaoziOpenclaw(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BaoziOpenclawImageDefault(self.pr, self._config)

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

        # Strip ANSI escape codes
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_re.sub("", test_log)

        for line in test_log.splitlines():
            line_stripped = line.strip()

            # --- TAP format (node:test via tsx, PR 22) ---
            # ok 1 - test name
            # not ok 2 - test name
            tap_ok = re.match(
                r"^\s*ok\s+\d+\s*-?\s*(.+?)(?:\s*#\s*(.*))?$", line
            )
            if tap_ok:
                test_name = tap_ok.group(1).strip()
                directive = tap_ok.group(2) or ""
                if "SKIP" in directive.upper() or "TODO" in directive.upper():
                    skipped_tests.add(test_name)
                else:
                    if test_name not in failed_tests:
                        passed_tests.add(test_name)
                continue

            tap_not_ok = re.match(
                r"^\s*not ok\s+\d+\s*-?\s*(.+?)(?:\s*#\s*(.*))?$", line
            )
            if tap_not_ok:
                test_name = tap_not_ok.group(1).strip()
                directive = tap_not_ok.group(2) or ""
                if "TODO" in directive.upper():
                    skipped_tests.add(test_name)
                else:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

            # --- Jest file-level format (PR 20) ---
            # PASS test/monitor.test.ts
            # FAIL test/monitor.test.ts
            jest_file_pass = re.match(
                r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$", line_stripped
            )
            if jest_file_pass:
                name = jest_file_pass.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            jest_file_fail = re.match(
                r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$", line_stripped
            )
            if jest_file_fail:
                name = jest_file_fail.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            # Vitest verbose: ✓ path > suite > test Nms (check BEFORE Jest individual — both use ✓/✕)
            vitest_pass = re.match(
                r"^\s*[\u2713\u2714]\s+(.+>.+?)\s+\d+m?s$", line
            )
            if vitest_pass:
                name = vitest_pass.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            vitest_fail = re.match(
                r"^\s*[\u00d7\u2717\u2715]\s+(.+>.+?)(?:\s+\d+m?s)?$", line
            )
            if vitest_fail:
                name = vitest_fail.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            # Jest individual: ✓ test name (5 ms) / ✕ test name (5 ms)
            jest_pass = re.match(
                r"^\s*[\u2713\u2714]\s+(.+?)(?:\s+\(\d+\s*ms\))?$", line
            )
            if jest_pass:
                name = jest_pass.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            jest_fail = re.match(
                r"^\s*[\u00d7\u2717\u2715]\s+(.+?)(?:\s+\(\d+\s*ms\))?$", line
            )
            if jest_fail:
                name = jest_fail.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            # --- Custom emoji format (PRs 21, 68) ---
            # ✅ Parsing test passed
            # ❌ Test name: error message
            custom_pass = re.match(r"^\s*\u2705\s+(.+?)$", line)
            if custom_pass:
                name = custom_pass.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            custom_fail = re.match(r"^\s*\u274c\s+(.+?)$", line)
            if custom_fail:
                name = custom_fail.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

            # "Test failed:" line (PR 21 failure mode)
            if line_stripped.startswith("Test failed:"):
                name = line_stripped[len("Test failed:"):].strip()
                if name:
                    failed_tests.add(name)
                continue

            # SKIP
            skip_match = re.match(r"SKIP:?\s?(.+?)\s", line_stripped)
            if skip_match:
                skipped_tests.add(skip_match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
