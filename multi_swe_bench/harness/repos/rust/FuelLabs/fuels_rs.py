"""FuelLabs/fuels-rs harness — modern era (PRs #1042–#1644).

Covers PRs where .github/workflows/ci.yml has all three env vars:
RUST_VERSION, FORC_VERSION, FUEL_CORE_VERSION.

This is the default registration (number_interval == "").
"""

from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FuelsRsImageBase(Image):
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
        return "rust:1.93.0"

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

        # Single shared base for every PR: clones the repo ONCE and installs the
        # system libraries the workspace links against. It deliberately does NOT
        # check out ``${BASE_COMMIT}`` and does NOT prune git history -- one
        # ``base`` tag is shared by every PR but each PR has a different
        # ``base.sha``, so pinning here strips every other PR's commit out of
        # history and their per-PR checkout dies with "unable to read tree".
        # The checkout and the history hardening happen in the per-PR image.
        #
        # Always clones, ignoring ``config.need_clone``: the ``COPY <repo>
        # /home/<repo>`` form is rewritten by ``_standardize_repo_fetch`` into the
        # very BASE_COMMIT-pinned sequence this image has to avoid.
        #
        # Two DockerfileEnhancer interactions (image.py) are load-bearing here,
        # because this image's dependency() is a str and so it IS processed:
        #   * ``_standardize_repo_fetch`` rewrites a hardcoded ``git clone <url>``
        #     into a BASE_COMMIT-pinned sequence. Its Pattern-2 regex carries a
        #     negative lookahead on the literal ``"${REPO_URL}"`` (injected as an
        #     ARG by the infrastructure block), so cloning against that literal
        #     leaves this layer untouched.
        #   * ``_inject_final_sanitize`` appends a BASE_COMMIT-pinned hardening
        #     block before the final CMD unless the marker line already appears
        #     between the clone and that CMD -- the comment below is that marker.
        #     The trailing CMD is required: with no CMD anywhere in the file the
        #     block is appended unconditionally and the marker is never consulted.
        return f"""FROM {image_name}

{self.global_env}

RUN rustup target add wasm32-unknown-unknown

RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}


{self.clear_env}

CMD ["/bin/bash"]
"""


class FuelsRsImageDefault(Image):
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
        return FuelsRsImageBase(self.pr, self.config)

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

# Extract forc and fuel-core versions from CI config at this commit.
#
# Two shapes exist across the eras this config spans. Modern PRs declare
# `env: FORC_VERSION: x.y.z`. Mid-era PRs (~#238-#347) declare no env var and
# instead install through actions-rs/cargo:
#     - name: Install Forc
#       with:
#         command: install
#         args: forc --version 0.11.0
# Missing the second shape leaves forc uninstalled, and since
# `build-test-projects` shells out to the `forc` binary to emit each test
# project's out/debug/*-abi.json (never committed), every abigen! expansion
# then fails and the whole harness stops compiling in all three phases.
FORC_VERSION=$(grep 'FORC_VERSION:' .github/workflows/ci.yml | head -1 | sed 's/.*: *//' | tr -d ' "'"'"'')
if [ -z "${{FORC_VERSION}}" ]; then
    FORC_VERSION=$(grep -E 'args: *forc --version' .github/workflows/ci.yml \
        | head -1 | sed -E 's/.*--version[[:space:]]+//' | tr -d ' "'"'"'')
fi

FUEL_CORE_VERSION=$(grep 'FUEL_CORE_VERSION:' .github/workflows/ci.yml | head -1 | sed 's/.*: *//' | tr -d ' "'"'"'')
if [ -z "${{FUEL_CORE_VERSION}}" ]; then
    FUEL_CORE_VERSION=$(grep -E 'args: *fuel-core --version' .github/workflows/ci.yml \
        | head -1 | sed -E 's/.*--version[[:space:]]+//' | tr -d ' "'"'"'')
fi

echo "Installing forc '${{FORC_VERSION}}' and fuel-core '${{FUEL_CORE_VERSION}}'"

# Detect architecture for binary downloads.
ARCH="$(dpkg --print-architecture)"
case "${{ARCH}}" in
    amd64) FUEL_ARCH="x86_64-unknown-linux-gnu" ;;
    arm64) FUEL_ARCH="aarch64-unknown-linux-gnu" ;;
    *) echo "Unsupported arch: ${{ARCH}}" && exit 1 ;;
esac

# Install forc (uses dpkg arch: amd64/arm64 for the release asset name).
#
# Early-era PRs pin no toolchain in ci.yml (the workflow just runs `cargo test`)
# and the Sway compiler is pulled in as the `forc` *crate* instead, so both
# greps above come back empty. Skip the install in that case rather than
# requesting ".../releases/download/v/..." and swallowing the 404: a masked
# failure here silently disables every `command -v forc` guarded step below.
#
# Prefer the prebuilt release, fall back to building from source: not every
# pinned version ships binaries (v0.11.0, used by PR #238, publishes none for
# any arch), which is exactly why upstream CI runs `cargo install forc`.
if [ -n "${{FORC_VERSION}}" ]; then
    if curl -sSfL "https://github.com/FuelLabs/sway/releases/download/v${{FORC_VERSION}}/forc-binaries-linux_${{ARCH}}.tar.gz" \
            -o /tmp/forc.tar.gz 2>/dev/null; then
        tar xzf /tmp/forc.tar.gz --strip-components=2 -C /usr/local/bin
        rm -f /tmp/forc.tar.gz
    else
        echo "No forc ${{FORC_VERSION}} release binary for ${{ARCH}}; building from source."
        rm -f /tmp/forc.tar.gz
        cargo install forc --version "${{FORC_VERSION}}" --locked \
            || cargo install forc --version "${{FORC_VERSION}}" \
            || {{ echo "ERROR: forc ${{FORC_VERSION}} install failed" >&2; exit 1; }}
    fi
    command -v forc >/dev/null 2>&1 \
        || {{ echo "ERROR: forc ${{FORC_VERSION}} is not on PATH after install" >&2; exit 1; }}
    forc --version || true
else
    echo "No FORC_VERSION in ci.yml at this commit; skipping forc install."
fi

# Install fuel-core binary (uses Rust triple: x86_64-.../aarch64-...).
if [ -n "${{FUEL_CORE_VERSION}}" ]; then
    curl -sSfL "https://github.com/FuelLabs/fuel-core/releases/download/v${{FUEL_CORE_VERSION}}/fuel-core-${{FUEL_CORE_VERSION}}-${{FUEL_ARCH}}.tar.gz" \
        -o /tmp/fc.tar.gz \
        && tar xzf /tmp/fc.tar.gz --strip-components=1 -C /usr/local/bin \
        && rm /tmp/fc.tar.gz \
        || {{ echo "ERROR: fuel-core ${{FUEL_CORE_VERSION}} (${{FUEL_ARCH}}) install failed" >&2; exit 1; }}
else
    echo "No FUEL_CORE_VERSION in ci.yml at this commit; skipping fuel-core install."
fi

# Build Sway test projects so compiled artifacts are available for tests.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

# Generate lockfile. At most commits fuels-rs is a library workspace that does
# not commit Cargo.lock, so fresh resolution may pull incompatible newer crate
# versions and pinning it here is what keeps the build reproducible.
#
# Some commits DO commit one (PR #113's base among them) and their fix patch
# may edit it. Regenerating in that case rewrites the file out from under
# `git apply`, which then fails with
#     error: patch failed: Cargo.lock:151
#     error: Cargo.lock: patch does not apply
# and the entire fix phase is lost -- the report comes back with
# fix_patch_result.all_count == 0 and the instance is discarded as invalid.
if [ -f Cargo.lock ]; then
    echo "Cargo.lock is committed at this commit; leaving it untouched."
else
    cargo generate-lockfile
fi

# Pin transitive deps only when the standalone fuel-core-client crate exists
# in this PR's dep tree (modern PRs ~#1337+). Older PRs use fuel-core as a
# single crate and do not need these pins.
if cargo metadata --format-version=1 2>/dev/null | grep -q '"name":"fuel-core-client"'; then
    cargo update -p cynic --precise 3.10.0 || true
    cargo update -p cynic-proc-macros --precise 3.10.0 || true
    cargo update -p fuel-core-client --precise "${{FUEL_CORE_VERSION}}" || true
    cargo update -p async-graphql --precise 7.0.15 || true
    cargo update -p async-graphql-derive --precise 7.0.15 || true
    cargo update -p async-graphql-parser --precise 7.0.15 || true
    cargo update -p async-graphql-value --precise 7.0.15 || true
fi

cargo build --tests || true

# Warm the Sway dependency cache while we still have a network budget.
#
# The abigen integration tests compile Sway contracts through the `forc` *crate*
# (not the CLI), which resolves the `core` / `std` deps declared in each
# test project's Forc.toml via https://api.github.com/repos/.../tarball. That
# endpoint allows 60 requests/hour to unauthenticated clients and forc sends no
# Authorization header, so a token cannot be injected -- a busy host exhausts
# the budget and every Sway-compiling test dies with
#   CompilationError("Couldn't download dependency (\\"core\\"): ... status code 403")
# identically in all three phases, which drops them from every f2p/p2p bucket.
#
# forc returns a cached checkout before it ever touches the network (see
# `if out_dir.exists()` in forc's utils/dependency.rs), so fetching once here
# bakes ~/.forc into the image and the run/test/fix phases need no network.
#
# Deliberately bounded, at most one attempt and always under `timeout`:
#   * the quota resets hourly, so retrying inside a single build cannot recover
#     a budget that is already spent -- it only multiplies the cost;
#   * some PRs' suites block indefinitely at this point (no fuel-core node is
#     running), which would otherwise hang the image build forever.
# The budget is probed first so the common rate-limited case costs one request
# instead of a full test run. /rate_limit is itself exempt from the limit.
FORC_DEP_DIR="${{HOME}}/.forc"
GH_REMAINING="$(curl -sS --max-time 20 https://api.github.com/rate_limit 2>/dev/null \
    | tr -d ' \\n' \
    | sed -n 's/.*"core":{{[^}}]*"remaining":\\([0-9][0-9]*\\).*/\\1/p')"
: "${{GH_REMAINING:=0}}"

if [ -n "$(find "${{FORC_DEP_DIR}}" -type f 2>/dev/null | head -1)" ]; then
    echo "Sway dependency cache already warm."
elif [ "${{GH_REMAINING}}" -lt 10 ]; then
    echo "WARNING: only ${{GH_REMAINING}} unauthenticated api.github.com requests are" >&2
    echo "WARNING: left, so the Sway dependency cache warm is skipped. Tests that" >&2
    echo "WARNING: compile Sway contracts will fail identically in every phase and" >&2
    echo "WARNING: be excluded from the report." >&2
else
    echo "Warming Sway dependency cache (${{GH_REMAINING}} API requests available)..."
    timeout 600 cargo test --workspace --no-fail-fast >/dev/null 2>&1 || true
    if [ -z "$(find "${{FORC_DEP_DIR}}" -type f 2>/dev/null | head -1)" ]; then
        echo "WARNING: Sway dependency cache is still empty after the warm run." >&2
    fi
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Rebuild Sway artifacts in case source changed.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

# Bounded: tests that reach for a fuel-core node block instead of failing when
# none is running (cargo starts reporting "has been running for over 60
# seconds" and never returns), which would otherwise stall the phase forever.
# On expiry the results printed so far are still parsed from this log.
timeout --preserve-status 900 cargo test --workspace --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

# Rebuild Sway artifacts after applying test patch.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

# Bounded: tests that reach for a fuel-core node block instead of failing when
# none is running (cargo starts reporting "has been running for over 60
# seconds" and never returns), which would otherwise stall the phase forever.
# On expiry the results printed so far are still parsed from this log.
timeout --preserve-status 900 cargo test --workspace --no-fail-fast

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Rebuild Sway artifacts after applying patches.
if command -v forc >/dev/null 2>&1; then
    if [ -d "e2e" ]; then
        forc build --release --terse --path e2e || true
    fi
    if [ -f "scripts/build-test-projects/Cargo.toml" ]; then
        cargo run -p build-test-projects || true
    elif [ -f "packages/fuels/Forc.toml" ]; then
        forc build --path packages/fuels || true
    else
        find . -path '*/tests/*' -name 'Forc.toml' -print0 2>/dev/null | while IFS= read -r -d '' forc_toml; do
            forc build --path "$(dirname "$forc_toml")" || true
        done
    fi
fi

# Bounded: tests that reach for a fuel-core node block instead of failing when
# none is running (cargo starts reporting "has been running for over 60
# seconds" and never returns), which would otherwise stall the phase forever.
# On expiry the results printed so far are still parsed from this log.
timeout --preserve-status 900 cargo test --workspace --no-fail-fast

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

        # The repo is cloned once in the shared base image, so this layer does NOT
        # clone: it only checks out this PR's commit in the inherited working tree.
        #
        # This per-PR image chains to a base *Image* (not a str), so
        # DockerfileEnhancer returns this dockerfile verbatim and does NOT
        # auto-inject git-history hardening. We therefore check out
        # ``${BASE_COMMIT}`` and apply ``Image._HARDENING_BLOCK`` manually so the
        # fix and later commits cannot be read out of git history (reward hacking).
        # ``BASE_COMMIT`` is pinned to *this* PR's ``base.sha``, which also prunes
        # the full history inherited from the shared base.
        #
        # Ordering matters: prepare.sh runs while git history is still intact (it
        # reads .github/workflows/ci.yml at the base commit and builds the Sway
        # test projects), and the hardening block runs immediately after.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

{prepare_commands}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# A test that writes to stdout without a trailing newline leaves cargo's result
# token glued to the end of that output line, e.g.
#     ... Ok (MyStruct :: new_from_tokens (& [token])) } } }test foo ... ok
# Several code_gen::abigen tests print generated code this way. Which tests are
# affected varies run to run with thread interleaving, so a line-anchored match
# silently loses a different subset in each phase and turns stable pass-to-pass
# tests into phantom none-to-pass ones. Split the token back onto its own line.
_GLUED_RESULT_RE = re.compile(
    r"^(.*\S)(test .+? \.\.\. (?:ok|FAILED|ignored)(?:,[^\n]*)?)$", re.MULTILINE
)

# The name is captured with (.+?) rather than (\S+) so names containing spaces
# survive: "<name> - should panic" and doc-tests such as
# "fuels-core/src/json_abi.rs - json_abi::ABIParser::encode (line 31)".
_TEST_RESULT_RE = re.compile(
    r"^test (.+?) \.\.\. (ok|FAILED|ignored)(?:,[^\n]*)?$", re.MULTILINE
)

_SHOULD_PANIC_SUFFIX = " - should panic"


def _parse_cargo_test_log(test_log: str) -> TestResult:
    """Parse standard ``cargo test`` output.

    Handles ANSI escape codes, result tokens glued to a test's own stdout,
    ``- should panic`` names and doc-tests, and deduplicates worst-wins.
    """
    test_log = _ANSI_RE.sub("", test_log)

    # Loop: a single line can carry more than one glued token, and the pattern
    # is end-anchored so each pass peels off the last one. Terminates because
    # every substitution splits one line into two.
    while True:
        unglued = _GLUED_RESULT_RE.sub(r"\1\n\2", test_log)
        if unglued == test_log:
            break
        test_log = unglued

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {
        "ok": passed_tests,
        "FAILED": failed_tests,
        "ignored": skipped_tests,
    }

    for match in _TEST_RESULT_RE.finditer(test_log):
        name = match.group(1).strip()
        # cargo reports a #[should_panic] test as "<name> - should panic"; strip
        # the suffix so the recorded name is the test's real path.
        if name.endswith(_SHOULD_PANIC_SUFFIX):
            name = name[: -len(_SHOULD_PANIC_SUFFIX)]
        if name:
            buckets[match.group(2)].add(name)

    # Deduplicate — worst result wins.
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


@Instance.register("FuelLabs", "fuels-rs")
class FuelsRs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return FuelsRsImageDefault(self.pr, self._config)

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
        return _parse_cargo_test_log(test_log)
