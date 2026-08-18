import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# There is no root Cargo.toml at either base commit, so tests must be driven
# per top-level crate. `soroban/` is excluded on purpose: it targets
# soroban-sdk 23 and needs the wasm32 target plus the stellar CLI, and no PR in
# this dataset touches it.
CRATE_DIRS = ["bounty_escrow", "grainlify-core", "program-escrow"]

# Some PRs committed `target/` build artifacts, which git records as binary
# diffs without full index lines and which therefore abort the whole `git
# apply`. Excluding them keeps the source hunks applicable.
_APPLY = "git apply --whitespace=nowarn --exclude='*target/*'"

CRATE_MARKER = "=== MSB_CRATE:"


def apply_patches(*patches: str) -> str:
    paths = " ".join(patches)
    # `cargo test` in prepare.sh generates lock files that are untracked at the
    # base commit, and git apply refuses to create a file that already exists.
    # The --3way fallback covers patches whose recorded pre-image blob is not
    # the one at base.sha (squashed or rebased upstream history).
    return f"""git checkout -- .
git clean -fxq -- '*Cargo.lock'
{_APPLY} {paths} || {_APPLY} --3way {paths}
"""


class GrainlifyStellarContractsImageBase(Image):
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
        return "rust:latest"

    def image_tag(self) -> str:
        # Keyed on base.sha because BASE_COMMIT is the base image's only
        # PR-dependent input, so PRs sharing a base commit build identical
        # images (4 PRs, 2 shas -> 2 builds).
        #
        # Do NOT collapse this to a constant "base": images dedupe by
        # image_full_name() into a set, so one tag keeps one arbitrary Image,
        # and BASE_COMMIT is read off whichever object survives. Other PRs
        # would then silently evaluate against the wrong tree. Keying on
        # base.sha makes every tag pin exactly one commit.
        return f"base-{self.pr.base.sha[:12]}"

    def workdir(self) -> str:
        # Constant while image_tag varies: the context is only the Dockerfile
        # (files() is empty, base images skip copy_source_code), and it is
        # identical per commit because BASE_COMMIT is a build arg.
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Do NOT add a `# syntax=` directive here, and do NOT rewrite the clone
        # as `RUN git clone "${REPO_URL}"`: either change makes the harness
        # DockerfileEnhancer skip this image, dropping the proxy/CA-cert/OCI
        # hardening and the BASE_COMMIT checkout (the bdk regression).
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y git pkg-config libssl-dev libcurl4-openssl-dev cmake && rm -rf /var/lib/apt/lists/*

# Toolchain pin is load-bearing: `rust:latest` (1.9x) fails to build the
# pinned `ethnum 1.5.2` (E0512 transmute size mismatch), while 1.81 is too old
# for the `edition2024` deps of program-escrow/grainlify-core. Only 1.86.0
# builds every in-scope crate at both base commits.
RUN rustup toolchain install 1.86.0 --profile minimal --component cargo && rustup default 1.86.0

WORKDIR /home/

{code}

{self.clear_env}

"""


class GrainlifyStellarContractsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return GrainlifyStellarContractsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        # `|| true` is required: without it the first crate whose tests fail
        # would abort the loop under `set -e` and hide every later crate.
        return f"""for crate in {" ".join(CRATE_DIRS)}; do
    [ -f "$crate/Cargo.toml" ] || continue
    echo "{CRATE_MARKER} $crate ==="
    (cd "$crate" && cargo test --workspace --no-fail-fast 2>&1) || true
done
"""

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

{test_cmd}
""".format(pr=self.pr, test_cmd=self.test_command()),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=self.test_command()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{apply_cmd}{test_cmd}
""".format(
                    pr=self.pr,
                    apply_cmd=apply_patches("/home/test.patch"),
                    test_cmd=self.test_command(),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{apply_cmd}{test_cmd}
""".format(
                    pr=self.pr,
                    apply_cmd=apply_patches("/home/test.patch", "/home/fix.patch"),
                    test_cmd=self.test_command(),
                ),
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


@Instance.register("Grainlify", "Grainlify-Stellar-Contracts")
class GrainlifyStellarContracts(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrainlifyStellarContractsImageDefault(self.pr, self._config)

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

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]
        re_crate_marker = re.compile(rf"^{re.escape(CRATE_MARKER)} (\S+) ===$")

        crate = ""

        for line in clean_log.splitlines():
            line = line.strip()

            marker = re_crate_marker.match(line)
            if marker:
                crate = marker.group(1)
                continue

            def qualify(name: str) -> str:
                return f"{crate}::{name}" if crate else name

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(qualify(match.group(1)))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(qualify(match.group(1)))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(qualify(match.group(1)))

        # Deduplicate — worst result wins
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
