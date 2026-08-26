import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OsoImageBase(Image):
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
        # Pinned: the repo has no rust-toolchain file, and Cargo.lock is from
        # early 2021 (proc-macro2 1.0.24, syn 1.0.58, criterion 0.3.3, half 1.6.0).
        # Those pins do not survive a current rustc, so `rust:latest` is out.
        # 1.75 is the sweet spot: old enough for the 2018-edition sources and the
        # locked crates, new enough that cargo defaults to the sparse crates.io
        # registry (>= 1.70) instead of cloning the multi-GB git index.
        return "rust:1.75"

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

        # Base image must stay plain: no `# syntax=` directive and a literal
        # clone URL (not "${REPO_URL}"). Either would disable DockerfileEnhancer,
        # dropping proxy/CA-cert injection, `git checkout ${BASE_COMMIT}`, and the
        # history-hardening block. See harness/image.py::DockerfileEnhancer.
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y git build-essential pkg-config && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class OsoImageDefault(Image):
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
        return OsoImageBase(self.pr, self.config)

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

# Warm the cargo registry and target dir. The repo root is the cargo workspace
# (members: polar-core, polar-c-api, polar-wasm-api, languages/rust/oso,
# languages/rust/oso-derive) and `oso` is the only member either patch touches,
# so build just that package and its path deps.
cargo test -p oso --lib --tests --no-run || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
cargo test -p oso --lib --tests --no-fail-fast 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cargo test -p oso --lib --tests --no-fail-fast 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cargo test -p oso --lib --tests --no-fail-fast 2>&1

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


@Instance.register("osohq", "oso")
class Oso(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OsoImageDefault(self.pr, self._config)

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

        re_pass = re.compile(r"test (\S+) \.\.\. ok")
        re_fail = re.compile(r"test (\S+) \.\.\. FAILED")
        re_skip = re.compile(r"test (\S+) \.\.\. ignored")

        # Integration tests are printed as a bare fn name with no binary context,
        # and `oso` has a genuine collision: `test_anything_works` is defined in
        # both tests/test_polar.rs and tests/test_polar_rust.rs. Prefix each name
        # with the binary cargo announces so the two stay distinct. Only the
        # stable part is captured — the `-<hash>` suffix changes per build and
        # would desync names across the run/test/fix stages.
        #     Running tests/test_oso.rs (target/debug/deps/test_oso-9a1b2c3d)
        #     Running unittests src/lib.rs (target/debug/deps/oso-4e5f6a7b)
        re_binary = re.compile(
            r"Running (?:tests/(\S+\.rs)|unittests \S+ \(target/debug/deps/(\S+?)(?:-[0-9a-f]+)?\))"
        )

        current_binary = ""
        for line in clean_log.splitlines():
            line = line.strip()

            bin_match = re_binary.search(line)
            if bin_match:
                current_binary = (bin_match.group(1) or bin_match.group(2)) + "::"

            match = re_pass.match(line)
            if match:
                passed_tests.add(current_binary + match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(current_binary + match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(current_binary + match.group(1))

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
