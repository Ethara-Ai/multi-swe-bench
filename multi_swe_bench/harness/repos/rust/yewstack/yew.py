from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GECKODRIVER_VERSION = "0.36.0"

# Exported once and shared by all three run scripts so the baseline, test-patch
# and fix-patch stages cannot drift apart -- differing commands would make the
# f2p comparison meaningless.
#
# RUSTFLAGS is deliberately NOT set here. .cargo/config.toml pins
# rustflags = ['--cfg', 'getrandom_backend="wasm_js"'] for wasm32, and an
# environment RUSTFLAGS *replaces* rather than appends to that, which silently
# breaks the wasm build.
_RUN_ENV = """export CI=true
export RUST_BACKTRACE=1
export GECKODRIVER="$(which geckodriver)\""""

# yew's test suite is wasm-only: every file in packages/yew-router/tests/ is
# gated behind `#![cfg(target_arch = "wasm32")]` and aliases wasm_bindgen_test
# as `test`, so a native `cargo test` compiles them out and reports zero tests.
# This is the command yew's own CI runs (.github/workflows/main-checks.yml);
# wasm-bindgen-test-runner (wired up in .cargo/config.toml) drives headless
# Firefox via geckodriver.
_TEST_CMD = """cd packages/yew-router
cargo test --target wasm32-unknown-unknown 2>&1"""


class YewImageBase(Image):
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
        # Pinned rather than `rust:latest` so rebuilds are reproducible. When the
        # repo ships a rust-toolchain.toml, prepare.sh's `rustup show` installs
        # that toolchain instead and this tag only acts as the floor.
        return "rust:1.93.0"

    def image_tag(self) -> str:
        # Per-PR rather than a shared "base": image dedup keys off
        # image_full_name(), so a bare "base" tag would make a second PR with a
        # different base commit silently reuse the first PR's image.
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

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {image_name}
{global_block}
WORKDIR /home/

RUN DEBIAN_FRONTEND=noninteractive apt-get update && \\
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
    cmake \\
    pkg-config \\
    libssl-dev \\
    curl \\
    wget \\
    bzip2 \\
    firefox-esr \\
    && rm -rf /var/lib/apt/lists/*

# yew's tests are wasm-bindgen browser tests, so the image needs the wasm
# target and a headless browser it can drive.
RUN rustup target add wasm32-unknown-unknown

# Firefox rather than Chrome: Mozilla publishes geckodriver for amd64 AND
# arm64, so this stays multi-arch. The arch is detected, never hardcoded.
ARG GECKODRIVER_VERSION={_GECKODRIVER_VERSION}
RUN set -eux; \\
    arch="$(dpkg --print-architecture)"; \\
    case "$arch" in \\
      amd64) slug="linux64" ;; \\
      arm64) slug="linux-aarch64" ;; \\
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \\
    esac; \\
    wget -qO /tmp/gd.tar.gz \\
      "https://github.com/mozilla/geckodriver/releases/download/v${{GECKODRIVER_VERSION}}/geckodriver-v${{GECKODRIVER_VERSION}}-${{slug}}.tar.gz"; \\
    tar -xzf /tmp/gd.tar.gz -C /usr/local/bin; \\
    chmod +x /usr/local/bin/geckodriver; \\
    rm -f /tmp/gd.tar.gz; \\
    geckodriver --version

{code}
{clear_block}"""


class YewImageDefault(Image):
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
        return YewImageBase(self.pr, self.config)

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
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# Install the toolchain the repo pins, if it pins one.
if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
    rustup show
fi

# wasm-bindgen-cli must match the `wasm-bindgen` version in Cargo.lock exactly
# or the test runner aborts. yew's own CI script derives the version from the
# lockfile, so it cannot drift as the base commit changes.
bash ci/install-wasm-bindgen-cli.sh
wasm-bindgen --version

{_RUN_ENV}

# Warm the cargo registry and build cache for the wasm target. `|| true`
# because a dependency that fails to compile here must not abort the image
# build -- the run stages report it instead.
cargo test -p yew-router --target wasm32-unknown-unknown --no-run 2>&1 || true

""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}

{_RUN_ENV}

{_TEST_CMD}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch

{_RUN_ENV}

{_TEST_CMD}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{_RUN_ENV}

{_TEST_CMD}

""",
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

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}
{global_block}
{copy_commands}
{prepare_commands}
{clear_block}"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# libtest result line: `test router::tests::trailing_slash ... ok`. The dots are
# escaped -- unescaped they are wildcards that would also match unrelated lines.
_RE_TEST_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<status>ok|FAILED|FAIL|ignored)\b"
)
# Native libtest prints "... FAILED"; the wasm-bindgen browser runner prints
# "... FAIL". Both must be recognised, and FAILED must precede FAIL in the
# alternation above so the longer token wins. Missing "FAIL" silently drops
# every wasm failure -- the failing tests vanish instead of being recorded,
# which empties f2p and makes a good instance look signal-free.
_FAIL_STATUSES = frozenset({"FAILED", "FAIL"})
# Cargo announces each test binary before executing it. yew's suite runs many
# binaries, and integration tests print bare function names with no module path,
# so without this label two same-named tests in different files collapse into
# one id.
#
# The leading \s+ is load-bearing and must NOT be relaxed to \s*: cargo indents
# its header ("     Running tests/foo.rs (...)"), while wasm-bindgen prints an
# UNindented "Running headless tests in Firefox on `http://127.0.0.1:45909/`"
# immediately after it. Matching both would overwrite every real binary label
# with a garbage one derived from the driver URL. Matched against the
# un-stripped line for exactly this reason.
_RE_RUNNING = re.compile(r"^\s+Running\s+(?P<rest>.+?)\s*$")
_RE_UNITTESTS = re.compile(r"^unittests\b")
_RE_DOCTESTS = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
# Strips cargo's build hash, and any extension after it -- wasm targets name the
# binary "yew_router-fef4fe4e95d0182b.wasm", so anchoring on $ alone would leave
# the hash embedded in every test id. A hash that shifted between the run/test/
# fix stages would make the same test look like two different tests and corrupt
# the f2p classification.
_RE_DEP_HASH = re.compile(r"-[0-9a-fA-F]{6,}(?:\.[A-Za-z0-9]+)?$")
# rustdoc names a doc-test after the source line it starts on; the line number is
# not part of the test's identity and shifts whenever a patch adds lines above.
_RE_DOCTEST_LINE_SUFFIX = re.compile(r"\s*\(line \d+\)\s*$")


def _binary_stem(running_rest: str) -> str:
    """Hash-free stem of the executable named in a cargo `Running ...` line.

    Handles both spellings:
        Running unittests src/lib.rs (target/debug/deps/yew-77f9765aa21e4a2a)
        Running target/debug/deps/yew-77f9765aa21e4a2a       (older cargo)
    """
    m = re.search(r"\(([^()]+)\)\s*$", running_rest)
    token = m.group(1) if m else running_rest.split()[-1]
    stem = token.replace("\\", "/").rsplit("/", 1)[-1]
    return _RE_DEP_HASH.sub("", stem) or stem


def _target_label(running_rest: str) -> str:
    rest = running_rest.strip()
    if _RE_UNITTESTS.match(rest):
        return _binary_stem(rest)
    head = rest.split(" (", 1)[0].strip()
    if head.endswith(".rs"):
        return head
    return _binary_stem(rest)


@Instance.register("yewstack", "yew")
class YewstackYew(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return YewImageDefault(self.pr, self._config)

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

        # Current test binary; empty until cargo announces one, in which case
        # ids fall back to the bare libtest name rather than being dropped.
        label = ""

        for raw in test_log.splitlines():
            unstripped = _RE_ANSI.sub("", raw)
            line = unstripped.strip()

            m = _RE_DOCTESTS.match(line)
            if m:
                label = f"doc-tests {m.group('crate')}"
                continue

            # Indentation-sensitive -- see the _RE_RUNNING comment.
            m = _RE_RUNNING.match(unstripped)
            if m:
                label = _target_label(m.group("rest"))
                continue

            m = _RE_TEST_LINE.match(line)
            if not m:
                continue

            name = m.group("name").strip()
            if label.startswith("doc-tests"):
                name = _RE_DOCTEST_LINE_SUFFIX.sub("", name)
            test_id = f"{label}::{name}" if label else name
            status = m.group("status")

            if status == "ok":
                passed_tests.add(test_id)
            elif status in _FAIL_STATUSES:
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        # A test that failed anywhere is failed, even if an earlier line said ok
        # (a retried or re-reported case), so TestResult's disjointness
        # invariants always hold.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
