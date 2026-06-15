import re
from typing import Optional

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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the helper scripts + patches into /home/ and runs
        # prepare.sh, which installs node_modules + builds so the eval scripts
        # run offline. node_modules lives inside the repo but is untracked, so
        # the hardening pass (which only rewrites git history) leaves it intact.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# installs dependencies and builds so the eval runs don't need network.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

npm install --legacy-peer-deps >/dev/null 2>&1 || true

# devDependencies introduced by the fix patch must be available to the test-only
# stage too (the test patch may import a devDep whose package.json entry lives in
# fix.patch). Apply just package.json from the fix, install, then revert the
# source -- node_modules is untracked so it survives the hardening pass.
if [ -f /home/fix.patch ]; then
    git apply --include=package.json --whitespace=nowarn --ignore-whitespace /home/fix.patch 2>/dev/null || true
    npm install --legacy-peer-deps >/dev/null 2>&1 || true
    git checkout -- package.json 2>/dev/null || true
fi

npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn --ignore-whitespace --exclude=package-lock.json --exclude=yarn.lock --exclude=index.js --exclude=validator.js --exclude=validator.min.js --exclude='lib/*' --exclude='es/*' /home/test.patch /home/fix.patch
npm install --legacy-peer-deps >/dev/null 2>&1 || true
npm run build > /home/build.log 2>&1 || {{ cat /home/build.log; exit 1; }}
./node_modules/.bin/mocha --reporter spec

""".format(pr=self.pr),
            ),
        ]


@Instance.register("validatorjs", "validator_js_0_to_899")
class ValidatorJs0To899(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        lines = test_log.splitlines()
        current_path = []
        indentation_to_level = {}

        for line in lines:
            line = ansi_escape.sub("", line)

            match = re.match(
                r"^(\s*)(?:([✓✔]|[0-9]+\))\s+)?(.*?)(?:\s+\([0-9]+ms\))?$", line
            )

            if not match or not match.group(3).strip():
                continue

            spaces, status, name = match.groups()
            name = name.strip()
            indent = len(spaces)

            if indent not in indentation_to_level:
                if not indentation_to_level:
                    indentation_to_level[indent] = 0
                else:
                    prev_indents = sorted(
                        [i for i in indentation_to_level.keys() if i < indent]
                    )
                    if prev_indents:
                        closest_indent = prev_indents[-1]
                        indentation_to_level[indent] = (
                            indentation_to_level[closest_indent] + 1
                        )
                    else:
                        indentation_to_level[indent] = 0

            level = indentation_to_level[indent]
            current_path = current_path[:level]
            current_path.append(name)

            if status:
                full_path = ":".join(current_path)
                if status in ("✓", "✔"):
                    passed_tests.add(full_path)
                elif status.endswith(")"):
                    failed_tests.add(full_path)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
