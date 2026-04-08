import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "node:18"

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
                "prepare.sh",
                """#!/bin/bash
cd /home/{pr.repo}
sed -i 's|npm.cubos.io|registry.npmjs.org|g' yarn.lock 2>/dev/null || true
yarn install --ignore-engines || npm install || true
yarn build || true
if [ -f "node_modules/.bin/lerna" ]; then
    node_modules/.bin/lerna bootstrap 2>/dev/null || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}

if [ -d "test" ]; then
    NODE_ENV=test ./node_modules/.bin/mocha --timeout 10000 || true
elif [ -f "node_modules/.bin/lerna" ]; then
    node_modules/.bin/lerna run test --stream || true
elif [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test /home/{pr.repo}/node_modules/.bin/mocha --timeout 60000 || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || true

sed -i 's|npm.cubos.io|registry.npmjs.org|g' yarn.lock 2>/dev/null || true

yarn install --ignore-engines 2>/dev/null || npm install 2>/dev/null || true

cd /home/{pr.repo}/node_modules/deasync 2>/dev/null && node-gyp rebuild 2>/dev/null || true
cd /home/{pr.repo}

sed -i 's/--exit//g' test/mocha.opts 2>/dev/null || true

yarn build || true

if [ -d "test" ]; then
    NODE_ENV=test ./node_modules/.bin/mocha --timeout 10000 || true
elif [ -f "node_modules/.bin/lerna" ]; then
    node_modules/.bin/lerna run test --stream || true
elif [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test /home/{pr.repo}/node_modules/.bin/mocha --timeout 60000 || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true

sed -i 's|npm.cubos.io|registry.npmjs.org|g' yarn.lock 2>/dev/null || true

yarn install --ignore-engines 2>/dev/null || npm install 2>/dev/null || true

cd /home/{pr.repo}/node_modules/deasync 2>/dev/null && node-gyp rebuild 2>/dev/null || true
cd /home/{pr.repo}

sed -i 's/--exit//g' test/mocha.opts 2>/dev/null || true

yarn build || true

if [ -d "test" ]; then
    NODE_ENV=test ./node_modules/.bin/mocha --timeout 10000 || true
elif [ -f "node_modules/.bin/lerna" ]; then
    node_modules/.bin/lerna run test --stream || true
elif [ -d "packages/core/integration-tests" ]; then
    cd packages/core/integration-tests
    NODE_ENV=test /home/{pr.repo}/node_modules/.bin/mocha --timeout 60000 || true
    cd /home/{pr.repo}
fi

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """FROM node:18

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get clean && apt-get update && apt-get install -y git build-essential python3
RUN ln -sf /usr/bin/python3 /usr/bin/python
RUN npm install -g cross-env lerna

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh

"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("parcel-bundler", "parcel_3649_to_22")
class PARCEL_3649_TO_22(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Mocha spec reporter output patterns
        # Pass: "  ✓ test name (Xms)" or "  ✓ test name"
        # Fail: "  N) test name" (numbered failures)
        # Pending/Skip: "  - test name"
        passed_pattern = re.compile(r"\s*[\u2713\u2714]\s+(.*)")
        failed_pattern = re.compile(r"\s*\d+\)\s+(.*)")
        skipped_pattern = re.compile(r"\s*-\s+(.*?)$")

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        lines = log.splitlines()
        i = 0
        while i < len(lines):
            line = ansi_escape.sub("", lines[i])
            stripped = line.strip()
            if not stripped:
                i += 1
                continue

            passed_match = passed_pattern.search(line)
            if passed_match:
                passed_tests.add(passed_match.group(1).strip())
                i += 1
                continue

            failed_match = failed_pattern.search(line)
            if failed_match:
                test_name = failed_match.group(1).strip()
                current_indent = len(line) - len(line.lstrip(" "))
                if i + 1 < len(lines):
                    next_line = ansi_escape.sub("", lines[i + 1])
                    next_indent = len(next_line) - len(next_line.lstrip(" "))
                    if next_indent > current_indent:
                        test_name += " " + next_line.strip().rstrip(":")
                        i += 1
                failed_tests.add(test_name)
                if test_name in passed_tests:
                    passed_tests.remove(test_name)
                i += 1
                continue

            skipped_match = skipped_pattern.search(line)
            if skipped_match:
                test_name = skipped_match.group(1).strip()
                if (
                    test_name
                    and test_name not in passed_tests
                    and test_name not in failed_tests
                ):
                    skipped_tests.add(test_name)

            i += 1

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
