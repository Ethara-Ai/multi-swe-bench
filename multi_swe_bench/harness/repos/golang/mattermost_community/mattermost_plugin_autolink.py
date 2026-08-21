import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AutolinkImageBase(Image):
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
        # go.mod declares `go 1.12`, but that is a floor and the module resolves
        # against mattermost-server/v6, which needs a modern toolchain. golang:1.21
        # was confirmed to build the tree and run the full suite at base
        # e73e585c717c843780960fcc441573955e6f7699.
        return "golang:1.21"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>` rather than a shared `base`: the Dockerfile QC
        # contract requires the PR layer to inherit
        # `mswebench/<org>_m_<repo>:base-pr-<N>`, and a shared tag hides a real
        # hazard -- this image bakes in one BASE_COMMIT, so a reused `base` stays
        # pinned to whichever PR built it first and any later PR whose base commit
        # is unreachable from that sha dies in prepare.sh. Costs one base image per
        # PR instead of one per repo; deliberate.
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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class AutolinkImageDefault(Image):
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
        return AutolinkImageBase(self.pr, self.config)

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
# Download modules ahead of time so the test stages are hermetic.
go mod download || true
# Warm the build cache so the graded stages only pay for compilation of the
# patched packages. Failure here is non-fatal to the target tests.
go test -v -count=1 ./... || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
go test -v -count=1 ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./...
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
go test -v -count=1 ./...
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


@Instance.register("mattermost-community", "mattermost-plugin-autolink")
class Mattermost_plugin_autolink(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AutolinkImageDefault(self.pr, self._config)

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
        # Strip ANSI escapes first (harmless for non-TTY `go test`, required if present).
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        run_tests = set(re.findall(r"=== RUN\s+([\S]+)", test_log))
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", test_log))
        failed_tests = set(re.findall(r"--- FAIL: ([\S]+)", test_log))
        # Test-level `--- SKIP:` plus package-level `[no test files]`.
        skipped_tests = set(re.findall(r"--- SKIP: ([\S]+)", test_log))
        skipped_tests |= set(
            re.findall(r"\?   \t([^\t]+?)\[no test files\]", test_log)
        )

        # A `=== RUN` with no terminal `--- PASS/FAIL/SKIP` line means the test
        # started but never finished (panic / fatal). Count those as failures.
        unterminated = run_tests - passed_tests - failed_tests - skipped_tests
        failed_tests |= unterminated

        # Enforce TestResult invariants: the three sets must be pairwise disjoint.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
