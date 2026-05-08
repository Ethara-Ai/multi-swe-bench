import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .joplin import JoplinImageBase, _CHECK_GIT_CHANGES_SH

_NODE_IMAGE = "node:10"
_INTERVAL_NAME = "joplin_1002_to_3978"


class ImageDefaultEra1(Image):

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
        return JoplinImageBase(
            self.pr, self._config, _NODE_IMAGE, _INTERVAL_NAME
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install || true

# Compile TypeScript files (mermaid.ts, sanitize_html.ts etc) -> .js
if [ -f "gulpfile.js" ]; then
    npx gulp build 2>&1 || npx tsc -p tsconfig.json 2>&1 || true
elif [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
npm install || true

mkdir -p build/locales
if [ -d "../ReactNativeClient/locales" ]; then
    cp -a ../ReactNativeClient/locales/* build/locales/ 2>/dev/null || true
fi
if [ -d "locales" ]; then
    cp -a locales/* build/locales/ 2>/dev/null || true
fi

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}/CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --reject /home/test.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true

# Re-install deps in case patches added new packages
npm install || true
cd CliClient && npm install || true
cd /home/{repo}

if [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn --exclude='package-lock.json' --reject /home/test.patch /home/fix.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true

# Re-install deps in case patches added new packages
npm install || true
cd CliClient && npm install || true
cd /home/{repo}

if [ -f "tsconfig.json" ]; then
    npx tsc -p tsconfig.json 2>&1 || true
fi

cd CliClient
if [ -f "gulpfile.js" ]; then
    npm test 2>&1 || true
elif [ -f "run_test.sh" ]; then
    bash run_test.sh 2>&1 || true
else
    npm test 2>&1 || true
fi

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("laurent22", _INTERVAL_NAME)
class JoplinEra1(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEra1(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # Jasmine 3.x default reporter: "N) suite_name test_name" under "Failures:"
        # Summary: "N specs, M failures, K pending"
        re_jasmine_failure = re.compile(r"^\d+\)\s+(.+)$")
        re_jasmine_summary = re.compile(
            r"^(\d+)\s+specs?,\s+(\d+)\s+failures?(?:,\s+(\d+)\s+pending)?$"
        )

        # Jest PASS/FAIL for gulp-era PRs that internally use jest
        re_jest_pass = re.compile(
            r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
        )
        re_jest_fail = re.compile(
            r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
        )

        # run_test.sh runs: `npm test tests-build/XYZ.js` — track which file
        re_npm_test_file = re.compile(r"^>\s+.*jasmine.*?(tests-build/\S+\.js)", re.IGNORECASE)

        in_failures_section = False
        total_specs = 0
        total_failures = 0
        total_pending = 0
        current_test_file = ""

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # Track which test file is being run (from npm output line)
            m = re_npm_test_file.match(line)
            if m:
                current_test_file = m.group(1)
                continue

            if line == "Failures:":
                in_failures_section = True
                continue

            m = re_jasmine_summary.match(line)
            if m:
                specs = int(m.group(1))
                failures = int(m.group(2))
                pending = int(m.group(3)) if m.group(3) else 0
                total_specs += specs
                total_failures += failures
                total_pending += pending
                in_failures_section = False

                if failures == 0 and current_test_file:
                    passed_tests.add(current_test_file)
                elif failures > 0 and current_test_file:
                    failed_tests.add(current_test_file)
                    passed_tests.discard(current_test_file)
                current_test_file = ""
                continue

            if in_failures_section:
                m = re_jasmine_failure.match(line)
                if m:
                    name = m.group(1).strip()
                    failed_tests.add(name)
                continue

            # Jest PASS/FAIL (gulp-era PRs)
            m = re_jest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re_jest_fail.match(line)
            if m:
                name = m.group(1).strip()
                failed_tests.add(name)
                passed_tests.discard(name)
                continue

        # If we have jasmine summaries but no file-level pass tracking,
        # generate synthetic pass names from the summary counts
        if total_specs > 0 and not passed_tests:
            jasmine_passed = total_specs - total_failures - total_pending
            for i in range(jasmine_passed):
                passed_tests.add("spec_{i}".format(i=i + 1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=total_pending if total_specs > 0 else len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
