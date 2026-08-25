from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# keylime/rust-keylime PR #736 ("crypto: Improve error handling and move to
# library"). Cargo workspace (members: keylime, keylime-agent,
# keylime-ima-emulator). The fix MOVES the crypto module from keylime-agent
# into the `keylime` library, bringing 16 `#[test]` fns with it
# (test/src/crypto.rs: RSA/AES/HMAC/KDF — OpenSSL-based, NO TPM at runtime).
# The test.patch adds keylime/test-data/test-rsa.pem, renames test-rsa.sig
# agent->library, and adds `--test-threads=1` to the repo's coverage run.
#
# Because the 16 crypto tests only exist in the `keylime` crate AFTER the fix,
# this is an N2P signal (none->pass), captured by scoping the run to
# `cargo test -p keylime` (the library crate).
#
# Build requirements (the load-bearing part): the `keylime` crate depends
# unconditionally on `tss-esapi` (TPM 2.0 bindings, generated via bindgen) and
# `openssl` (0.10, links system OpenSSL). So the crate does not compile without
# libssl-dev + libtss2-dev + clang/libclang (bindgen) + pkg-config. The crypto
# tests themselves do not touch a TPM, so no TPM emulator is needed at run time.


class RustKeylimeImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        # rust-keylime @ this era uses actix-web 4 / clap 4.3 / tss-esapi
        # (edition 2021). rust:1.75-bookworm is a compatible toolchain, and
        # Debian bookworm's apt has libtss2-dev + libclang for the tss-esapi
        # bindgen build.
        return "rust:1.75-bookworm"

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

        # No `# syntax=` directive and a literal clone URL: both are required for
        # DockerfileEnhancer to rewrite the clone into the pinned checkout +
        # history-pruning block.
        return f"""FROM {image_name}

{self.global_env}

ENV RUST_BACKTRACE=1
ENV DEBIAN_FRONTEND=noninteractive

# tss-esapi needs the TSS2 dev headers + libclang (bindgen); the openssl crate
# needs libssl-dev + pkg-config. build-essential/clang for the native builds.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential pkg-config libssl-dev libtss2-dev clang libclang-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class RustKeylimeImageDefault(Image):
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
        return RustKeylimeImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Warm-build the library crate + its test harness (compiles tss-esapi/openssl
# and all deps once, so the graded runs are fast). This is ALSO a hard gate: if
# the baseline `keylime` crate cannot compile here, the system deps
# (libssl-dev/libtss2-dev/libclang) are missing and the build must fail loudly
# rather than silently produce 0/0/0 later.
cargo build -p keylime --tests

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
cargo test -p keylime -- --test-threads=1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch \
  || git apply --3way --whitespace=nowarn /home/test.patch || true
cargo test -p keylime -- --test-threads=1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch \
  || git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch || true
cargo test -p keylime -- --test-threads=1

""".format(repo=self.pr.repo),
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


@Instance.register("keylime", "rust-keylime")
class RustKeylime(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RustKeylimeImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes before parsing.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # cargo test reports lines like `test crypto::tests::test_hash ... ok`
        # / `... FAILED` / `... ignored`.
        re_pass = re.compile(r"test (\S+) \.\.\. ok\b")
        re_fail = re.compile(r"test (\S+) \.\.\. FAILED\b")
        re_skip = re.compile(r"test (\S+) \.\.\. ignored\b")

        for line in test_log.splitlines():
            line = line.strip()

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue
            m = re_skip.match(line)
            if m:
                skipped_tests.add(m.group(1))
                continue

        # Deduplicate — worst result wins; enforce TestResult disjointness.
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
