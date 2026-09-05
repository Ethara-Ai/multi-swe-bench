from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# This module covers the `crates/` era of swc - PR 2601 onwards. The flat
# pre-reorganisation layout (669..2129) lives in swc_2129_to_669.py.

# Environment shared by prepare.sh / run.sh / test-run.sh / fix-run.sh.
#
# Every base commit in this range pins a pre-1.68 nightly (nightly-2021-11-15,
# nightly-2022-01-19), so cargo has no sparse registry support and must clone the
# crates.io git index. Fetching that with the system git binary instead of
# libgit2 is the difference between a slow clone and a hung one.
_ENV_BLOCK = r"""export CI=1
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1
export RUST_LOG=off
"""

# Package selection. In this layout a crate's directory name under `crates/` is
# its cargo package name, so the packages under test can be read straight off the
# test patch.
_PACKAGES_BLOCK = r"""cargo_test_args() {
    CRATES=""
    if [ -d crates ]; then
        CRATES=$(grep '^diff --git a/crates/' /home/test.patch 2>/dev/null \
                 | sed 's|diff --git a/crates/\([^/]*\)/.*|\1|' | sort -u)
    fi

    if [ -n "$CRATES" ]; then
        ARGS=""
        for c in $CRATES; do
            ARGS="$ARGS -p $c"
        done
    else
        # Safety net only - every PR in this range resolves at least one crate.
        # `--all-features` is deliberately not used: it turns on the node binding
        # and wasm members' feature sets, which do not build on the host.
        ARGS="--workspace"
    fi
    echo "$ARGS"
}
"""

# The test invocation shared by run.sh / test-run.sh / fix-run.sh, so the three
# stages are byte-for-byte identical apart from the patches applied beforehand.
_CARGO_TEST_BLOCK = r"""# Enumerate a package's integration test targets so each can be run on its own.
list_test_targets() {
    cargo metadata --no-deps --format-version 1 2>/dev/null | node -e "
let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{
  try{
    const m=JSON.parse(s);
    for(const p of (m.packages||[])){
      if(p.name!=='$1') continue;
      for(const t of (p.targets||[])){
        if((t.kind||[]).includes('test')) console.log(t.name);
      }
    }
  }catch(e){}
});"
}

ARGS=$(cargo_test_args)
echo "=== Testing packages:$ARGS ==="

LOG=$(mktemp)
# Per-target invocation rather than one `cargo test -p pkg`.
#
# Several PRs in this dataset write a test that uses API the *fix* patch adds
# (2129 -> es2015::new_target, 2601 -> config::IsModule). In the test stage that
# file cannot compile, and because cargo builds a package's test targets as one
# unit the failure takes down every other test in the package too - the stage
# reports 0/0/0 and the ~600 tests that would have run vanish. Running each
# target separately confines the damage to the one broken target, so the rest
# still report real numbers.
set +e
if [ "$ARGS" = "--workspace" ]; then
    cargo test --workspace --no-fail-fast 2>&1 | tee -a "$LOG"
else
    for PKG in $(printf '%s\n' $ARGS | grep -v '^-p$'); do
        cargo test -p "$PKG" --lib --no-fail-fast 2>&1 | tee -a "$LOG"
        for T in $(list_test_targets "$PKG"); do
            echo "--- $PKG --test $T ---"
            cargo test -p "$PKG" --test "$T" --no-fail-fast 2>&1 | tee -a "$LOG"
        done
    done
fi
set -e
if ! grep -q '^test result:' "$LOG"; then
    echo "=== BUILD FAILURE: no test binary ran ==="
fi
rm -f "$LOG"
exit 0
"""


class SwcImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # Pinned rather than `rust:latest`: the host rustc version is irrelevant
    # because every base commit overrides it through its rust-toolchain file, so
    # what this tag actually fixes is the distro (bookworm, for the node packages
    # below) and the rustup that installs those pinned nightlies.
    def dependency(self) -> str | Image:
        return "rust:1.83-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        # NOTE: the leading `# syntax=` directive marks this Dockerfile as
        # already carrying the standard infrastructure block, so
        # DockerfileEnhancer leaves the layout below untouched. Beyond the
        # structure-only clone, this base installs node: swc's `exec.js` fixtures
        # are compiled and then run with it (swc_ecma_transforms_testing calls
        # `Command::new("node")`), so without it those tests fail in every stage.
        # No checkout, no history rewriting - those belong to the PR image.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    cmake \\
    libssl-dev \\
    nodejs \\
    npm \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{global_block}
WORKDIR /home/

{code}

WORKDIR /home/{repo}
{clear_block}
CMD ["/bin/bash"]
"""


class SwcImageDefault(Image):
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
        return SwcImageBase(self.pr, self.config)

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
                r"""#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{env}
{packages}
# Install the correct Rust toolchain if rust-toolchain or rust-toolchain.toml exists
if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
    rustup show
fi

# Initialize submodules (test262-parser-tests, terser, etc.)
git submodule update --init --recursive 2>&1 || true

# The transform `exec.js` fixtures are compiled and then executed with node, and
# the regenerator / async-to-generator suites resolve `regenerator-runtime` out
# of node_modules, so the JS dependencies have to exist before any test runs.
# node_modules is gitignored, so the reset at the end of this script leaves it in
# place.
if [ -f package.json ]; then
    if [ -f yarn.lock ]; then
        npx --yes yarn install --frozen-lockfile 2>&1 \
            || npx --yes yarn install 2>&1 \
            || true
    else
        npm install 2>&1 || true
    fi
fi

warm() {{
    cargo test $(cargo_test_args) --no-run --no-fail-fast 2>&1 || true
}}

# Warm the dependency graph at the base commit, then once more with both patches
# applied, so the run/test/fix stages only ever recompile the crates that the
# patches actually touch.
warm

git apply --whitespace=nowarn /home/test.patch 2>&1 || true
git apply --whitespace=nowarn /home/fix.patch 2>&1 || true
warm

git reset --hard
git clean -fd -e node_modules

""".format(pr=self.pr, env=_ENV_BLOCK, packages=_PACKAGES_BLOCK),
            ),
            File(
                ".",
                "run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

{env}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi

{env}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                r"""#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "=== PATCH FAILURE: test.patch did not apply ==="
    exit 1
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    echo "=== PATCH FAILURE: fix.patch did not apply ==="
    exit 1
fi

{env}
{packages}
{block}
""".format(
                    pr=self.pr,
                    env=_ENV_BLOCK,
                    packages=_PACKAGES_BLOCK,
                    block=_CARGO_TEST_BLOCK,
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

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        # BASE_COMMIT is a build-scope ARG of the base stage and does not survive
        # into this one, so the hardening block is emitted with the literal sha.
        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}
{global_block}
{copy_commands}
WORKDIR /home/{self.pr.repo}

{hardening}

RUN bash /home/prepare.sh
{clear_block}"""


@Instance.register("swc-project", "swc")
class SwcProjectSwc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SwcImageDefault(self.pr, self._config)

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
        # Strip ANSI first: CARGO_TERM_COLOR=never covers cargo's own output, but
        # a panic message or a test that writes colour of its own would otherwise
        # slip past the patterns below.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # A libtest name is only unique inside its own binary, and `cargo test -p
        # a -p b` puts several crates' output in one stream, so every name is
        # qualified with the target that produced it. Both the current
        # `Running unittests src/lib.rs (target/debug/deps/foo-1a2b3c)` form and
        # the older `Running target/debug/deps/foo-1a2b3c` form are recognised;
        # the trailing metadata hash is dropped so a name stays identical across
        # the run / test / fix stages.
        re_running_paren = re.compile(r"^Running\s+.*\(([^()]+)\)$")
        re_running_plain = re.compile(r"^Running\s+(\S+)$")
        re_doc_tests = re.compile(r"^Doc-tests\s+(\S+)$")
        re_hash = re.compile(r"-[0-9a-f]{7,}$")

        re_pass_tests = [re.compile(r"test (\S+) \.\.\. ok")]
        re_fail_tests = [re.compile(r"test (\S+) \.\.\. FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) \.\.\. ignored")]

        target = ""
        for line in log.splitlines():
            line = line.strip()

            match = re_running_paren.match(line) or re_running_plain.match(line)
            if match:
                target = re_hash.sub("", match.group(1).rsplit("/", 1)[-1])
                continue

            match = re_doc_tests.match(line)
            if match:
                target = f"doc::{match.group(1)}"
                continue

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    name = match.group(1)
                    passed_tests.add(f"{target}::{name}" if target else name)

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    name = match.group(1)
                    failed_tests.add(f"{target}::{name}" if target else name)

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    name = match.group(1)
                    skipped_tests.add(f"{target}::{name}" if target else name)

        # TestResult.__post_init__ raises on a name that lands in two buckets,
        # which a retried or flaky test can otherwise produce. Failure wins.
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
