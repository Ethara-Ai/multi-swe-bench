import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class JavascriptImageBase(Image):
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
        return "node:14-bullseye"

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

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class JavascriptImageDefault(Image):
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
        return JavascriptImageBase(self.pr, self.config)

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
set -e

cd /home/{pr.repo}
git reset --hard
git checkout {pr.base.sha}

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
EXERCISE_DIRS=$(node /home/setup-exercise.js)
if [ -z "$EXERCISE_DIRS" ]; then
    echo "ERROR: No exercise directories found in patches" >&2
    exit 1
fi
npx jest --verbose --no-cache $EXERCISE_DIRS 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
EXERCISE_DIRS=$(node /home/setup-exercise.js)
if [ -z "$EXERCISE_DIRS" ]; then
    echo "ERROR: No exercise directories found in patches" >&2
    exit 1
fi
npx jest --verbose --no-cache $EXERCISE_DIRS 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
EXERCISE_DIRS=$(node /home/setup-exercise.js)
if [ -z "$EXERCISE_DIRS" ]; then
    echo "ERROR: No exercise directories found in patches" >&2
    exit 1
fi
npx jest --verbose --no-cache $EXERCISE_DIRS 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "setup-exercise.js",
                "const fs = require('fs');\n"
                "const path = require('path');\n"
                "\n"
                "// Parse exercise directories from patch file diff headers\n"
                "const patches = ['/home/fix.patch', '/home/test.patch'];\n"
                "const exerciseDirs = new Set();\n"
                "\n"
                "for (const patchFile of patches) {\n"
                "    if (!fs.existsSync(patchFile)) continue;\n"
                "    const content = fs.readFileSync(patchFile, 'utf8');\n"
                "    for (const line of content.split('\\n')) {\n"
                "        if (!line.startsWith('diff --git')) continue;\n"
                "        const match = line.match(/exercises\\/\\w+\\/[^\\/]+/);\n"
                "        if (match) exerciseDirs.add(match[0]);\n"
                "    }\n"
                "}\n"
                "\n"
                "// Set up each exercise: copy exemplar to solution, enable xtest\n"
                "for (const dir of exerciseDirs) {\n"
                "    if (!fs.existsSync(dir)) continue;\n"
                "    if (!fs.existsSync(path.join(dir, '.meta', 'config.json'))) continue;\n"
                "\n"
                "    const config = JSON.parse(fs.readFileSync(path.join(dir, '.meta', 'config.json'), 'utf8'));\n"
                "    const files = config.files || config;\n"
                "    const exemplar = (files.example || files.exemplar || [])[0];\n"
                "    const solution = (files.solution || [])[0];\n"
                "\n"
                "    if (exemplar && solution) {\n"
                "        const src = path.join(dir, exemplar);\n"
                "        const dst = path.join(dir, solution);\n"
                "        if (fs.existsSync(src)) fs.copyFileSync(src, dst);\n"
                "    }\n"
                "\n"
                "    // Enable xtest -> test, xdescribe -> describe (like CI prepare)\n"
                "    // Do NOT replace test.skip or describe.skip (CI convention)\n"
                "    const specFiles = fs.readdirSync(dir).filter(f => f.endsWith('.spec.js'));\n"
                "    for (const file of specFiles) {\n"
                "        const filePath = path.join(dir, file);\n"
                "        let text = fs.readFileSync(filePath, 'utf8');\n"
                "        text = text.replace(/xtest\\(/g, 'test(');\n"
                "        text = text.replace(/xdescribe\\(/g, 'describe(');\n"
                "        fs.writeFileSync(filePath, text);\n"
                "    }\n"
                "}\n"
                "\n"
                "// Output space-separated exercise dirs for jest\n"
                "process.stdout.write([...exerciseDirs].join(' '));\n",
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


@Instance.register("exercism", "javascript")
class Javascript(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return JavascriptImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        clean_log = ansi_re.sub("", test_log)

        current_file = ""
        describe_stack = []
        in_error_section = False

        for line in clean_log.splitlines():
            stripped = line.strip()

            if not stripped:
                continue

            file_match = re.match(r"^\s*(PASS|FAIL)\s+(\S+)", line)
            if file_match:
                current_file = file_match.group(2).strip()
                describe_stack = []
                in_error_section = False
                continue

            # jest error detail section: ● describe › test name
            if stripped.startswith("\u25cf"):
                in_error_section = True
                continue

            if in_error_section:
                continue

            if re.match(
                r"^(Test Suites:|Tests:|Snapshots:|Time:|Ran all test suites)",
                stripped,
            ):
                continue

            if not current_file:
                continue

            indent = len(line) - len(line.lstrip())

            # jest verbose: ✓ passed, ✕ failed, ○ skipped/todo
            pass_match = re.match(
                r"^\s+[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if pass_match:
                test_name = pass_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                passed_tests.add(full_name)
                continue

            fail_match = re.match(
                r"^\s+[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line
            )
            if fail_match:
                test_name = fail_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                failed_tests.add(full_name)
                continue

            skip_match = re.match(
                r"^\s+○\s+(?:skipped|todo)\s+(.+?)\s*$", line
            )
            if skip_match:
                test_name = skip_match.group(1).strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                parts = [current_file] + [d[1] for d in describe_stack] + [test_name]
                full_name = " > ".join(parts)
                skipped_tests.add(full_name)
                continue

            if indent > 0:
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                describe_stack.append((indent, stripped))

        # dedup: worst result wins (failed > skipped > passed)
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
