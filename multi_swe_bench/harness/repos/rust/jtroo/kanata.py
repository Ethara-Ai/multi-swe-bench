import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# kanata pins no toolchain (no rust-toolchain file, no rust-version); CI uses
# the stable toolchain of the day. Rust 1.99.0 was stable on the base date of
# the PRs (2026-10). The workspace is edition 2024, so 1.85 is the hard floor.
_RUST_IMAGE = "rust:1.99.0-bookworm"

_ENV = """export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
# rust-lld (default x86_64 linker since 1.90) segfaults under QEMU amd64 emulation; use the system linker
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS='-C linker-features=-lld'"""

# One suite: the whole workspace with default features, as the repo's Linux CI
# runs it. Feature unification turns kanata/simulated_output on for this build
# (simulated_passthru's default features), so the sim_tests module that
# src/tests.rs gates on that feature is compiled and run here; a second
# `--features=simulated_output -- sim_tests` pass would only repeat it.
_SUITES = [
    ("workspace", "cargo test --workspace --locked --offline --no-fail-fast"),
]

_SUITE_MARK = "kanata-suite"
_LOCK_COPY = "/home/kanata.Cargo.lock"
_RESTORE_LOCK = f"cp {_LOCK_COPY} Cargo.lock"


def _test_commands() -> str:
    lines = ["rc=0"]
    for name, cmd in _SUITES:
        lines.append(f'echo "{_SUITE_MARK} {name}"')
        lines.append(f"{cmd} || rc=$?")
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


class KanataImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # libudev-dev: evdev/gilrs on Linux (the repo's CI installs it).
        return ["pkg-config", "libudev-dev", "libssl-dev"]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    RUST_BACKTRACE=1 \\\n"
            "    CI=true\n"
            "RUN git config --global --add safe.directory '*' && rustc --version && cargo --version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. This is a per-PR base, so the
    # pin and the history scrub belong here.


class KanataImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return KanataImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        tests = _test_commands()
        apply = _apply_flags(self.pr)
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}

# The committed Cargo.lock can lag Cargo.toml (at 4ec4cd35 it lacks the
# arboard wayland-data-control subtree), and the repo's CI never passes
# --locked. So the first fetch may add missing entries to Cargo.lock; existing
# pins are kept. Everything after this uses --locked --offline against it.
fetched=0
for attempt in 1 2 3 4 5; do
  if cargo fetch; then
    fetched=1
    break
  fi
  sleep 20
done
test "$fetched" = 1
# Keep the resolved lockfile outside the repo: the run scripts restore it, so
# they also work from a freshly reset tree (git reset --hard / checkout -- .).
cp Cargo.lock {_LOCK_COPY}

# Pre-build every test binary the run scripts use, at this commit.
cargo test --workspace --locked --offline --no-run

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
sim_list=$(cargo test --workspace --locked --offline -- sim_tests --list)
grep -qE 'sim_tests::.*: test$' <<< "$sim_list"
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_RESTORE_LOCK}
{tests}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_RESTORE_LOCK}
git apply {apply} /home/test.patch
{tests}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_RESTORE_LOCK}
git apply {apply} /home/test.patch /home/fix.patch
{tests}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("jtroo", "kanata")
class KANATA(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KanataImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        suite_re = re.compile(rf"^{re.escape(_SUITE_MARK)} (\S+)\s*$")
        # "Running unittests src/lib.rs (target/debug/deps/kanata_parser-0123abcd)"
        # "Running tests/foo.rs (target/debug/deps/foo-0123abcd)"
        running_re = re.compile(
            r"^Running (?:unittests )?(\S+) \((?:.*/)?([A-Za-z0-9_]+?)(?:-[0-9a-f]{8,})?(?:\.exe)?\)\s*$"
        )
        doctest_re = re.compile(r"^Doc-tests (\S+)\s*$")
        result_re = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")

        suite = ""
        target = ""
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            stripped = line.strip()
            found = suite_re.match(stripped)
            if found:
                suite = found.group(1)
                target = ""
                continue
            found = running_re.match(stripped)
            if found:
                target = f"{found.group(2)}[{found.group(1)}]"
                continue
            found = doctest_re.match(stripped)
            if found:
                target = f"{found.group(1)}[doc]"
                continue
            found = result_re.match(stripped)
            if not found or not target:
                continue
            name = re.sub(r" \(line \d+\)", "", found.group(1)).strip()
            test = f"{suite}/{target}::{name}" if suite else f"{target}::{name}"
            status = found.group(2)
            if status == "ok":
                passed_tests.add(test)
            elif status == "FAILED":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
