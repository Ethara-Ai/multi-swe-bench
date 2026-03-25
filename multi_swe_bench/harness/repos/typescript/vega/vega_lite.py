import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class VegaLiteImageBase(Image):
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
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
{code}

{self.clear_env}

"""


class VegaLiteImageDefault(Image):
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
        return VegaLiteImageBase(self.pr, self._config)

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

yarn install --frozen-lockfile --ignore-scripts --ignore-engines || yarn install --ignore-scripts --ignore-engines

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run_tests.sh",
                """#!/bin/bash
cd /home/{pr.repo}

if [ -f "vitest.config.ts" ] || [ -f "vitest.config.js" ] || [ -f "vitest.config.mjs" ]; then
    echo "=== Detected Vitest ==="
    npx vitest run test/ --reporter=verbose || true
elif grep -q '"jest-puppeteer"' package.json 2>/dev/null; then
    echo "=== Detected jest-puppeteer preset, overriding config ==="
    if [ -d "node_modules/ts-jest" ]; then
        JEST_CONFIG='{{"transform":{{"^.+\\\\.tsx?$":"ts-jest"}},"testEnvironment":"node","testPathIgnorePatterns":["node_modules","build","_site","src"]}}'
    else
        JEST_CONFIG='{{"testEnvironment":"node","testPathIgnorePatterns":["node_modules","build","_site","src"]}}'
    fi
    npx jest test/ --no-cache --verbose --no-coverage --config "$JEST_CONFIG" || true
else
    echo "=== Detected ts-jest ESM ==="
    NODE_OPTIONS=--experimental-vm-modules npx jest test/ --no-cache --verbose --no-coverage || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
yarn install --frozen-lockfile --ignore-scripts --ignore-engines || yarn install --ignore-scripts --ignore-engines
yarn build || true
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
yarn install --frozen-lockfile --ignore-scripts --ignore-engines || yarn install --ignore-scripts --ignore-engines
yarn build || true
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
yarn install --frozen-lockfile --ignore-scripts --ignore-engines || yarn install --ignore-scripts --ignore-engines
yarn build || true
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


@Instance.register("vega", "vega-lite")
class VegaLite(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VegaLiteImageDefault(self.pr, self._config)

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

        # Jest format patterns:
        #   PASS test/compile/scale/domain.test.ts (14.681 s)
        #   FAIL test/compile/selection/layers.test.ts
        #   ✓ should include x (5ms)
        #   × should handle error
        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[\u2713\u2714]\s+(.+)$"),
        ]

        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*s\))?$"),
            re.compile(r"^\s*[\u00d7\u2717\u2715]\s+(.+)$"),
        ]

        # Vitest format patterns:
        #   ✓ |unit| test/compile/scale/range.test.ts (16 tests) 101ms
        #   × |unit| test/compile/legend/assemble.test.ts (5 tests | 3 failed) 85ms
        #   FAIL  |unit| test/compile/legend/assemble.test.ts > legend/assemble > ...
        vitest_pass_re = re.compile(
            r"^\s*[\u2713\u2714]\s+\|[^|]+\|\s+(.+?)\s+\(\d+\s+tests?\)\s+\d+ms$"
        )
        vitest_fail_re = re.compile(
            r"^\s*[\u00d7\u2717\u2715]\s+\|[^|]+\|\s+(.+?)\s+\(\d+\s+tests?"
        )
        vitest_fail_line_re = re.compile(
            r"^\s*FAIL\s+\|[^|]+\|\s+(.+?)\s+>"
        )

        skipped_res = [re.compile(r"SKIP:?\s?(.+?)\s")]

        for line in test_log.splitlines():
            m = vitest_pass_re.match(line)
            if m:
                test_name = m.group(1).strip()
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = vitest_fail_re.match(line)
            if m:
                test_name = m.group(1).strip()
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue

            m = vitest_fail_line_re.match(line)
            if m:
                test_name = m.group(1).strip()
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue

            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))
                    break

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    passed_tests.discard(m.group(1))
                    break

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
