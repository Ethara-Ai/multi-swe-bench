import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NixpacksImageBase(Image):
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
        # The dataset covers a single PR (#334, merged 2022-07-19, base
        # cf31d0d6d68e8d646fdd3f78753d1325a7aadc7a). Cargo.toml at that commit
        # declares edition = "2021" and no rust-version / rust-toolchain.toml,
        # so the floor is 1.56. 1.62 is the toolchain contemporary with the PR
        # and reads the lockfile v3 Cargo.lock the fix patch updates. Pinned
        # rather than `rust:latest` so the image is reproducible.
        return "rust:1.62-bullseye"

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

        # docker.io provides the `docker` CLI: tests/docker_run_tests.rs (the
        # file the test patch touches) shells out to `docker build` / `docker run`
        # and needs a daemon socket mounted at run time. pkg-config + libssl-dev
        # cover the native build inputs of the crate graph on both amd64 and
        # arm64; no arch-specific source lists or downloads are used.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    pkg-config \\
    libssl-dev \\
    docker.io \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class NixpacksImageDefault(Image):
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
        return NixpacksImageBase(self.pr, self.config)

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

# Warm the cargo registry and compile every test target without executing it.
# `--no-run` is deliberate: tests/docker_run_tests.rs needs a Docker daemon,
# which is not available during `docker build`, so running the suite here would
# only burn time. `|| true` keeps a native-dep compile failure on arm64 from
# aborting the image build.
cargo test --all --no-run || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never

cd /home/{pr.repo}
cargo test --all --no-fail-fast -- --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cargo test --all --no-fail-fast -- --test-threads=1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1
export CARGO_TERM_COLOR=never

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cargo test --all --no-fail-fast -- --test-threads=1

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


@Instance.register("railwayapp", "nixpacks")
class Nixpacks(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NixpacksImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first: cargo colorizes even when piped on
        # some terminals, and the patterns below are anchored on plain text.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # `cargo test --all` runs several binaries (src unit tests,
        # tests/generate_plan_tests.rs, tests/docker_run_tests.rs, doc-tests)
        # and libtest prints the bare function name in each. Short names such as
        # `test_rust` recur across targets, so every name is prefixed with the
        # target cargo announces on its "Running" line. The hash suffix in
        # `target/debug/deps/<name>-<hash>` is stripped because it changes
        # between stages and would split one test into three Report entries.
        re_running = re.compile(r"^Running (?:unittests )?(\S+)(?: \((\S+)\))?$")
        re_doc = re.compile(r"^Doc-tests\s+(\S+)$")
        re_head = re.compile(r"^test (.+?) \.\.\.(.*)$")
        re_token = re.compile(r"^(ok|FAILED|ignored)\b")
        re_summary = re.compile(r"^test result:")

        def norm_target(raw: str) -> str:
            if raw.endswith(".rs"):
                return raw
            base = raw.rsplit("/", 1)[-1]
            return re.sub(r"-[0-9a-f]{8,}$", "", base)

        def classify(token: str) -> Optional[str]:
            if token.startswith("ok"):
                return "passed"
            if token.startswith("FAILED"):
                return "failed"
            if token.startswith("ignored"):
                return "skipped"
            return None

        def record(name: str, token: str) -> None:
            kind = classify(token)
            if kind == "passed":
                passed_tests.add(name)
            elif kind == "failed":
                failed_tests.add(name)
            elif kind == "skipped":
                skipped_tests.add(name)

        target = "unknown"
        pending: Optional[str] = None

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_running.match(line)
            if match:
                target = norm_target(match.group(2) or match.group(1))
                pending = None
                continue

            match = re_doc.match(line)
            if match:
                target = f"doc-tests-{match.group(1)}"
                pending = None
                continue

            match = re_head.match(line)
            if match:
                # `... ok` / `... FAILED` / `... ignored, <reason>`; only the
                # test name (group 1) is kept, never timing or counts, so the
                # same test yields the same name in all three stages.
                name = f"{target}::{match.group(1).strip()}"
                rest = match.group(2).strip()
                if classify(rest) is not None:
                    record(name, rest)
                    pending = None
                else:
                    # A child process (docker/git) can print between the header
                    # and the result token, leaving the token at the end of the
                    # polluted line or alone on a later line.
                    tail = rest.rsplit(" ", 1)[-1] if rest else ""
                    if re_token.match(tail):
                        record(name, tail)
                        pending = None
                    else:
                        pending = name
                continue

            if pending is not None:
                if re_token.match(line):
                    record(pending, line)
                    pending = None
                elif re_summary.match(line):
                    # Target finished without emitting this test's token.
                    pending = None

        # Deduplicate: ensure no test appears in multiple categories
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
