import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TsJsonSchemaGeneratorImageBase(Image):
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
{code}

{self.clear_env}

"""


class TsJsonSchemaGeneratorImageDefault(Image):
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
        return TsJsonSchemaGeneratorImageBase(self.pr, self._config)

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

# Detect package manager from lockfile
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile --ignore-engines || yarn install --ignore-engines
elif [ -f "package-lock.json" ]; then
    npm install
else
    npm install
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
cd /home/{pr.repo}

# Detect test runner: tsx --test (Node native TAP) vs jest
if grep -q 'tsx --test' package.json 2>/dev/null || grep -q 'node --test' package.json 2>/dev/null; then
    echo "=== Detected tsx --test (Node TAP runner) ==="
    bash -c 'shopt -s globstar && npx tsx --test test/**/*.test.ts' || true
else
    echo "=== Detected Jest ==="
    npx jest test/ --no-cache --verbose --no-coverage || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}

# Install dependencies
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile --ignore-engines || yarn install --ignore-engines
elif [ -f "package-lock.json" ]; then
    npm install
else
    npm install
fi

# Build (needed for some tests, e.g. minify tests reference dist/)
npm run build || true

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

# Install dependencies
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile --ignore-engines || yarn install --ignore-engines
elif [ -f "package-lock.json" ]; then
    npm install
else
    npm install
fi

# Build
npm run build || true

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

# Install dependencies
if [ -f "yarn.lock" ]; then
    yarn install --frozen-lockfile --ignore-engines || yarn install --ignore-engines
elif [ -f "package-lock.json" ]; then
    npm install
else
    npm install
fi

# Build
npm run build || true

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


@Instance.register("vega", "ts-json-schema-generator")
class TsJsonSchemaGenerator(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TsJsonSchemaGeneratorImageDefault(self.pr, self._config)

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

        # Detect TAP format (tsx --test / node --test)
        if "TAP version" in test_log:
            return self._parse_tap_log(test_log)

        # Jest format patterns:
        #   PASS test/unit/deepMerge.test.ts (14.681 s)
        #   FAIL test/minify/index.test.ts
        #   ✓ merges booleans with enums (2 ms)
        #   ✕ With minify (203 ms)
        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[\u2713\u2714]\s+(.+?)(?:\s+\(\d+\s*ms\))?$"),
        ]

        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[\u00d7\u2717\u2715]\s+(.+?)(?:\s+\(\d+\s*ms\))?$"),
        ]

        skipped_res = [re.compile(r"SKIP:?\s?(.+?)\s")]

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1).strip() not in failed_tests:
                    passed_tests.add(m.group(1).strip())
                    break

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    test_name = m.group(1).strip()
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                    break

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_tap_log(self, test_log: str) -> TestResult:
        """Parse Node TAP format output from tsx --test / node --test.

        TAP format:
            TAP version 13
            # Subtest: deepMerge
                # Subtest: merges booleans with enums
                ok 1 - merges booleans with enums
                not ok 2 - failing test
            1..2
            # tests 2
            # pass 1
            # fail 1
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Match individual test results (innermost subtests)
        tap_ok_re = re.compile(r"^\s*ok\s+\d+\s*-?\s*(.+?)(?:\s*#\s*(.*))?$")
        tap_not_ok_re = re.compile(r"^\s*not ok\s+\d+\s*-?\s*(.+?)(?:\s*#\s*(.*))?$")

        for line in test_log.splitlines():
            m = tap_ok_re.match(line)
            if m:
                test_name = m.group(1).strip()
                directive = m.group(2) or ""
                if "SKIP" in directive.upper() or "TODO" in directive.upper():
                    skipped_tests.add(test_name)
                else:
                    passed_tests.add(test_name)
                continue

            m = tap_not_ok_re.match(line)
            if m:
                test_name = m.group(1).strip()
                directive = m.group(2) or ""
                if "TODO" in directive.upper():
                    skipped_tests.add(test_name)
                else:
                    failed_tests.add(test_name)
                    passed_tests.discard(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
