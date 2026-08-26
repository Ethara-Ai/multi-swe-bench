import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class VscodeMarkdownNotesImageBase(Image):
    """Repo-level base: Node 18 + git. kortina/vscode-markdown-notes is a VS Code extension
    written in TypeScript and tested with jest. package.json pins no `engines.node`, so the
    constraint comes from the toolchain: jest 26 / ts-jest 26 / typescript 3.8. Node 18 is the
    newest LTS those still run under, and it is a maintained Debian base (D10 ships git and
    ca-certificates)."""

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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

{self.clear_env}

"""


class VscodeMarkdownNotesImageDefault(Image):
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
        return VscodeMarkdownNotesImageBase(self.pr, self._config)

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

""",
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

npm ci || npm install || true

# Warm the compile + jest caches at build time, where the network is available.
npx tsc -p ./ || true
npx jest --runInBand --forceExit --ci || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# jest.config.js sets testMatch to <rootDir>/out/test/jest/**/*.test.js, so jest runs the
# COMPILED output and tsc must run first. `|| true` belongs on tsc and only on tsc:
# tsconfig.json does not set noEmitOnError, so tsc still writes out/**/*.js when it reports
# type errors. That is load-bearing at the test stage -- the gold tests call
# NoteWorkspace.cleanPipedWikiLink, which the fix patch introduces, so tsc type-errors there
# but still emits, and the tests execute and fail at runtime on an undefined method. That is
# the FAIL the f2p transition needs. Aborting on the type error instead would collect zero
# tests and silently downgrade the instance to n2p.
npx tsc -p ./ || true
# --runInBand: jest's worker pool deadlocks under emulation and on memory-capped Docker VMs,
# and docker_util.run has no timeout, so a deadlock hangs the harness forever (R14).
# No `|| true` on this line -- the exit code is irrelevant but a runner that fails to START
# must not be masked into an empty log.
npx jest --runInBand --forceExit --ci --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npx tsc -p ./ || true
npx jest --runInBand --forceExit --ci --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npx tsc -p ./ || true
npx jest --runInBand --forceExit --ci --verbose

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("kortina", "vscode-markdown-notes")
class VscodeMarkdownNotes(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VscodeMarkdownNotesImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")
        re_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
        re_fail = re.compile(r"^\s*[✕×✗✘✖]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
        re_skip = re.compile(r"^\s*○\s+skipped\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")

        suite = ""

        for line in clean_log.split("\n"):
            suite_match = re_suite.match(line.strip())
            if suite_match:
                # jest reports the COMPILED path (out/test/jest/extension.test.js) because
                # jest.config.js matches <rootDir>/out. report.py's _test_name_matches_files
                # compares against the patch's repo-relative source path, so re-root the
                # compiled path back onto src/ and restore the .ts extension (R20). Done
                # identically in all three stages, so names stay stable across them (R3).
                path = suite_match.group(2)
                if path.startswith("out/"):
                    path = "src/" + path[len("out/") :]
                if path.endswith(".js"):
                    path = path[: -len(".js")] + ".ts"
                suite = path
                # The suite line is not a test -- never record it as one.
                continue

            for regex, bucket in (
                (re_pass, passed_tests),
                (re_fail, failed_tests),
                (re_skip, skipped_tests),
            ):
                match = regex.match(line)
                if match:
                    name = match.group(1).strip()
                    bucket.add(f"{suite} > {name}" if suite else name)
                    break

        # R2 -- the sets must be disjoint or TestResult raises. Failure wins.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
