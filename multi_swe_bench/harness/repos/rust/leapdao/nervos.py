import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `leapdao/nervos` is a two-package monorepo. Only `packages/parent-bridge` is
# Rust; `packages/childchain` is Solidity/truffle and no PR in this dataset
# touches it, so the harness stays inside the Rust package.
PKG_DIR = "packages/parent-bridge"

# Nervos CKB on-chain script. `capsule.toml` declares it as the single
# `template_type = "Rust"` contract of the package.
CONTRACT = "parent-bridge"

# `packages/parent-bridge/Cargo.toml` is a workspace whose only member is
# `tests` and which *excludes* `contracts`, so the contract and the test
# harness are two independent dependency graphs resolved by two lockfiles.
RISCV_TARGET = "riscv64imac-unknown-none-elf"

# Both graphs are vendored into one directory by `cargo vendor --sync`, and the
# emitted source-replacement config is written to the workspace root so the
# excluded contract crate picks it up through cargo's parent-directory walk.
VENDOR_DIR = "/home/vendor"

# The 2020 cargo that ships in the base image cannot fetch today's
# crates.io-index (a multi-GB git repo it clones in full and then fails on with
# "error reading from the zlib stream"). A modern cargo is installed purely to
# resolve and vendor the two lockfiles over the sparse index; every actual
# compile still runs on the pinned 2020 nightly.
VENDOR_TOOLCHAIN = "1.75.0"

# `capsule build` post-processes every Rust contract with ckb-binary-patcher,
# which rewrites the call-relaxation stubs LLD emits into a form CKB-VM 0.19
# (vendored by ckb 0.34.1) will execute. ckb-std's own Makefile installs this
# tool for the same reason, so it is kept here to stay faithful to `capsule
# build` -- but note it is *not* what fixes the `InvalidPermission` abort; see
# RUSTFLAGS below.
PATCHER_REPO = "https://github.com/xxuejie/ckb-binary-patcher.git"
PATCHER_REV = "b9489de4b3b9d59bc29bce945279bc6f28413113"

# Both flags are load-bearing.
#
# `--no-rosegment` is the difference between a usable report and an empty one.
# Left out, LLD emits a standalone read-only segment for .rodata and .text then
# starts at a *non-page-aligned* vaddr (0x144d4 here, i.e. 0x4d4 into the page
# at 0x14000). CKB-VM 0.19 loads such a segment by mapping the whole page and
# then zeroing the leading padding with
# `store_byte(aligned_start, padding_start, 0)` -- a data write issued *after*
# `convert_flags` has already marked that page FLAG_EXECUTABLE | FLAG_FREEZED.
# Its own W^X check then rejects the write, and `verify_tx` aborts with
# `Internal(VM(InvalidPermission))` before a single contract instruction runs.
# Every test in the suite fails identically in all three stages, so nothing
# transitions f2p and Report.check() rejects the instance. Folding .rodata into
# the text segment reproduces the single page-aligned R+X LOAD that the CKB C
# toolchain emits for every stock contract, always_success included.
#
# `-s` strips. CKB-VM bills program loading by binary size, and the unstripped
# artifact is 3.3 MB against 120 KB stripped -- enough to push these tests past
# their `MAX_CYCLES = 10_000_000` ceiling and fail for a reason that has
# nothing to do with the patch under test.
RUSTFLAGS = "-C link-arg=-s -C link-arg=--no-rosegment"


class NervosImageBase(Image):
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
        # The toolchain image capsule 0.1.x builds `template_type = "Rust"`
        # contracts in, which is what this package's README ("capsule build")
        # targets. It carries exactly the four things this repo needs and that
        # no current `rust:*` tag can supply together:
        #
        #   * nightly-2020-06-01 -- contracts/parent-bridge/src/main.rs opens
        #     with #![feature(lang_items)] / #![feature(alloc_error_handler)],
        #     so nightly is mandatory, and ckb-std 0.6.1 uses the pre-1.59
        #     `asm!` syntax that modern nightlies removed.
        #   * the riscv64imac-unknown-none-elf std component, pre-installed.
        #   * the RISC-V GNU toolchain at /riscv/bin for the `cc`-crate part of
        #     ckb-std.
        #   * OpenSSL 1.1.1 -- the tests crate pins openssl-sys 0.9.58 through
        #     ckb-tool, and 0.9.58 does not build against OpenSSL 3.x.
        return "jjy0/ckb-capsule-recipe-rust:2020-6-2"

    # Tagged per-PR rather than with a single shared `base` tag: one mutable
    # `:base` tag for the whole repo lets a later PR's build overwrite the
    # image this PR layer inherits, silently rebasing it onto another PR's
    # BASE_COMMIT.
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

        # Do NOT add a `# syntax=` directive and do NOT pre-write the clone as
        # `RUN git clone "${REPO_URL}"`: either makes DockerfileEnhancer bail
        # out of this Dockerfile, dropping the proxy/CA-cert/OCI hardening and
        # -- critically -- the `git checkout ${BASE_COMMIT}` it substitutes in
        # place of the hardcoded clone below.
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # No apt layer on purpose: the base image already provides git,
        # pkg-config, cc, curl, python3 and the OpenSSL headers, and bionic is
        # past EOL, so an `apt-get update` here would only add a network
        # dependency that can rot without buying anything.
        return f"""FROM --platform=linux/amd64 {image_name}

{self.global_env}

ENV PATH=/riscv/bin:$PATH
ENV CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse
ENV CARGO_NET_RETRY=5
ENV CARGO_TERM_COLOR=never
ENV RUST_BACKTRACE=1
ENV CI=true

RUN rustup toolchain install {VENDOR_TOOLCHAIN} --profile minimal

RUN git clone {PATCHER_REPO} /tmp/ckb-binary-patcher \
 && git -C /tmp/ckb-binary-patcher checkout {PATCHER_REV} \
 && cargo +{VENDOR_TOOLCHAIN} install --path /tmp/ckb-binary-patcher --root /usr/local \
 && rm -rf /tmp/ckb-binary-patcher

WORKDIR /home/

{code}

{self.clear_env}

"""


class NervosImageDefault(Image):
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
        return NervosImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        # The contract is rebuilt inside the *test* command, not once in
        # prepare.sh. fix.patch edits
        # contracts/parent-bridge/src/main.rs, and tests/src/lib.rs loads the
        # compiled artifact from `../build/debug/<name>` at runtime -- so a
        # contract built only in prepare.sh would leave fix-run.sh verifying
        # the unfixed binary and nothing would ever transition to PASS.
        #
        # cwd for the `tests` package is packages/parent-bridge/tests, which is
        # what makes Loader's `../build/debug` resolve to the copy below.
        return f"""cd /home/{self.pr.repo}/{PKG_DIR}

RUSTFLAGS="{RUSTFLAGS}" cargo build --offline --manifest-path contracts/{CONTRACT}/Cargo.toml --target {RISCV_TARGET}
mkdir -p build/debug
ckb-binary-patcher -i contracts/{CONTRACT}/target/{RISCV_TARGET}/debug/{CONTRACT} -o build/debug/{CONTRACT}

cargo test --offline -- --test-threads=1
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

cd /home/{pr.repo}/{pkg_dir}
# One vendor tree for both dependency graphs. `--sync` folds the excluded
# contract crate's lockfile into the same resolve.
#
# The emitted source-replacement config is written to $CARGO_HOME instead of
# <pkg>/.cargo/config so nothing lands inside the checked-out tree: .cargo/ is
# not in this repo's .gitignore, so writing it there left the image shipping a
# permanently dirty working tree and made the clean-tree assertions above
# unenforceable for the graded stages. $CARGO_HOME/config sits at the bottom of
# cargo's config hierarchy, below the directory walk, so it applies to both the
# workspace build and the excluded contract crate exactly as the in-tree file
# did -- and nothing overrides it, since no .cargo/config remains anywhere.
#
# `|| true` per harness convention -- a vendoring failure cannot be
# meaningfully recovered here and surfaces immediately as an empty
# fix_patch_result, which Report.check() rejects on rule 1.
CARGO_CFG_DIR="${{CARGO_HOME:-$HOME/.cargo}}"
mkdir -p "$CARGO_CFG_DIR"
cargo +{toolchain} vendor --versioned-dirs --sync contracts/{contract}/Cargo.toml {vendor_dir} > "$CARGO_CFG_DIR/config" || true

# Warm the target/ cache (~350 crates for the ckb 0.34.1 test harness) into the
# image layer so each of the three graded stages only recompiles the contract
# and the test crate. Wrapped in a function because test_command() is
# multi-line: a bare `{{test_cmd}} || true` would put `|| true` on its own line
# and be a bash syntax error, silently killing the warm-up.
warm_cache() {{
{test_cmd}
}}
warm_cache || true
""".format(
                    pr=self.pr,
                    pkg_dir=PKG_DIR,
                    contract=CONTRACT,
                    toolchain=VENDOR_TOOLCHAIN,
                    vendor_dir=VENDOR_DIR,
                    test_cmd=self.test_command(),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=self.test_command()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{test_cmd}
""".format(pr=self.pr, test_cmd=self.test_command()),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_cmd}
""".format(pr=self.pr, test_cmd=self.test_command()),
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


@Instance.register("leapdao", "nervos")
class Nervos(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NervosImageDefault(self.pr, self._config)

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

        # libtest emits one atomic line per test even when running
        # concurrently, and carries no timing or count metadata in it, so the
        # captured name is byte-identical across the run/test/fix stages.
        # `\S+` cannot swallow the `test result: ...` summary line.
        re_pass = re.compile(r"^test (\S+) \.\.\. ok$")
        re_fail = re.compile(r"^test (\S+) \.\.\. FAILED$")
        re_skip = re.compile(r"^test (\S+) \.\.\. ignored$")

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))

        # Worst result wins, so the TestResult set-disjointness invariants hold
        # even if a name is reported twice in one log.
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
