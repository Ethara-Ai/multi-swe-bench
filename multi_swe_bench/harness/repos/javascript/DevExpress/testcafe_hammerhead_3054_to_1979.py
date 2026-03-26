import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault(Image):
    # Skip syntax directive - causes buildkitd containerd worker to crash
    skip_syntax_directive = True

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
        return "node:14-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """ls -la
###ACTION_DELIMITER###
npm install
###ACTION_DELIMITER###
npx gulp test-server""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
npx gulp test-server

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
npx gulp test-server

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
npx gulp test-server

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM node:14-bullseye

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y git jq

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/DevExpress/testcafe-hammerhead.git /home/testcafe-hammerhead

WORKDIR /home/testcafe-hammerhead
RUN git reset --hard
RUN git checkout {pr.base.sha}

# Install dependencies (--legacy-peer-deps for old peer dep conflicts)
RUN npm install --legacy-peer-deps

# Dynamically select @types versions based on TypeScript version:
# - TS >= 4.0: @types/node@16.18.0 (for dns.setDefaultResultOrder)
# - TS >= 3.4: @types/node@14.0.27, @types/semver@6.x (supports readonly T[])
# - TS < 3.4: @types/node@14.0.27, @types/semver@5.x (no readonly syntax)
RUN TS_VER=$(jq -r '.devDependencies.typescript // "3.0.0"' package.json | sed 's/[^0-9.]//g') && \\
    TS_MAJOR=$(echo "$TS_VER" | cut -d. -f1) && \\
    TS_MINOR=$(echo "$TS_VER" | cut -d. -f2) && \\
    if [ "$TS_MAJOR" -ge 4 ] 2>/dev/null; then \\
        echo "TypeScript >= 4.x detected ($TS_VER)" && \\
        npm install @types/node@16.18.0 @types/lodash@4.14.182 @types/minimatch@3.0.5 --legacy-peer-deps; \\
    elif [ "$TS_MAJOR" -eq 3 ] && [ "$TS_MINOR" -ge 4 ] 2>/dev/null; then \\
        echo "TypeScript 3.4+ detected ($TS_VER)" && \\
        npm install @types/node@14.0.27 @types/lodash@4.14.182 @types/minimatch@3.0.5 --legacy-peer-deps; \\
    else \\
        echo "TypeScript < 3.4 detected ($TS_VER), using older @types/semver" && \\
        npm install @types/node@14.0.27 @types/lodash@4.14.182 @types/minimatch@3.0.5 @types/semver@5.5.0 --legacy-peer-deps; \\
    fi

# Remove transitive @types that conflict with project's own typings
RUN rm -rf node_modules/@types/http-cache-semantics node_modules/keyv/src/index.d.ts || true

# Add skipLibCheck to suppress remaining node_modules type noise
RUN jq '.compilerOptions.skipLibCheck = true' tsconfig.json > tmp.json && mv tmp.json tsconfig.json

# Fix brotli.ts type error in affected versions (returns Buffer instead of Promise<Buffer>)
# This is a source code bug in certain commits - add @ts-ignore to make compilation pass
RUN if [ -f src/processing/encoding/brotli.ts ]; then \\
        if grep -q "return hasBuiltInBrotliSupport" src/processing/encoding/brotli.ts 2>/dev/null; then \\
            sed -i 's/return hasBuiltInBrotliSupport/\\/\\/ @ts-ignore: type mismatch in original source\\n    return hasBuiltInBrotliSupport/g' src/processing/encoding/brotli.ts; \\
        fi; \\
    fi

# Fix connection-reset-guard.ts type error (Function vs callable signature)
RUN if [ -f src/request-pipeline/connection-reset-guard.ts ]; then \\
        if grep -q "fn: Function" src/request-pipeline/connection-reset-guard.ts 2>/dev/null; then \\
            sed -i 's/fn: Function/fn: () => void/g' src/request-pipeline/connection-reset-guard.ts; \\
        fi; \\
    fi

# Fix proxy/index.ts createServer union type issue in early TS 4.x PRs (2777-2792)
# Add explicit return type to _getCreateServerMethod
RUN if [ -f src/proxy/index.ts ]; then \\
        if grep -q "_getCreateServerMethod (ssl?:" src/proxy/index.ts 2>/dev/null; then \\
            sed -i 's/_getCreateServerMethod (ssl?: \\({{}}\\))/_getCreateServerMethod (ssl?: \\1): typeof http.createServer/g' src/proxy/index.ts; \\
        fi; \\
    fi

# Fix session/index.ts unused renaming error in TS 4.x (line with handleAttachment)
RUN if [ -f src/session/index.ts ]; then \\
        sed -i 's/handleAttachment ({{ isOpenedInNewWindow: boolean }})/handleAttachment (options: {{ isOpenedInNewWindow: boolean }})/g' src/session/index.ts 2>/dev/null || true; \\
    fi
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("DevExpress", "testcafe_hammerhead_3054_to_1979")
class TESTCAFE_HAMMERHEAD_3054_TO_1979(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped

        # Pattern for passed tests: ✓ followed by test name
        passed_re = re.compile(r"^\s*✓\s+(.*)$")
        # Pattern for failed tests: number) followed by test name
        failed_re = re.compile(r"^\s*\d+\)\s+(.*)$")
        for line in log.split("\n"):
            line = line.strip()
            # Check for passed tests
            passed_match = passed_re.match(line)
            if passed_match:
                test_name = passed_match.group(1).strip()
                passed_tests.add(test_name)
            # Check for failed tests
            failed_match = failed_re.match(line)
            if failed_match:
                test_name = failed_match.group(1).strip()
                failed_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
