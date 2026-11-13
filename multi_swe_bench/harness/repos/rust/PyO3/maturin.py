"""PyO3/maturin -- single crate (lib + `maturin` bin) with an integration-test
crate `tests/run.rs` that builds real wheels from `test-crates/*` fixtures.

Per-PR base (`base-pr-<N>`): the inherited Image.dockerfile() clones, pins
${BASE_COMMIT} and runs the hardening scrub; DockerfileEnhancer adds the
BuildKit directive, ARGs, proxy/TLS env and cert links.

Toolchain: Cargo.toml has `rust-version = "1.89"` (edition 2024) and no
rust-toolchain file at either base sha; CI uses `stable`. Rust 1.98.0 was the
stable release on the PRs' base dates (2026-09-23 / 2026-09-25).

The image is trixie, not bookworm: maturin's PGO flow creates its training venv
with `python -m venv` and installs PEP 735 dependency groups with
`pip install --group`, which needs pip >= 25.1. Debian trixie's python3.13
ensurepip/virtualenv seed pip 25.1; bookworm's seed pip 23.0. The official
`rust:<ver>-trixie` image is multi-arch (amd64 + arm64).

Test scope (same for every PR, offline at test time):
  * `cargo test --lib`: maturin's unit tests.
  * `cargo test --test run -- pgo:: integration::abi3_and_`: the PGO wheel
    builds and the combined stable-ABI wheel-selection builds -- the
    integration tests the PR test patches exercise. The rest of the
    integration suite (dozens of wheel builds, conda/uv/zig/uniffi-bindgen
    cases, sdist/develop flows) is excluded for runtime under load.
Features `faster-tests,schemars` follow CI (`password-storage` is left out: it
pulls keyring/D-Bus and no selected test uses it).

Offline guarantees: prepare.sh fetches the cargo deps of maturin and of every
test-crates fixture lockfile into CARGO_HOME, and downloads the Python packages
the wheel installs need (boltons, attrs, cffi + pycparser) into a wheelhouse
outside the repo. The run scripts set CARGO_NET_OFFLINE and PIP_NO_INDEX /
PIP_FIND_LINKS, so no download can happen while tests run. Nothing in prepare
modifies tracked files; build outputs land in git-ignored dirs (`target/`,
`test-crates/targets/`, `test-crates/venvs/`, `test-crates/wheels/`).
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.98.0-trixie"

_WHEELHOUSE = "/opt/maturin-wheelhouse"
# Pure-python except cffi (arch-specific wheel, downloaded on the build arch).
_PY_PACKAGES = ["boltons==25.0.0", "attrs==25.3.0", "cffi==2.0.0"]
# Fixtures built by the selected integration tests.
_FIXTURES = ["test-crates/pyo3-mixed", "test-crates/pyo3-abi3-and-abi3t"]

_ENV = f"""export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS=3
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_FIND_LINKS={_WHEELHOUSE}
# rust-lld (default x86_64 linker since 1.90) segfaults under QEMU amd64 emulation; use the system linker
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS='-C linker-features=-lld'"""

_RUN_ENV = """export CARGO_NET_OFFLINE=true
export PIP_NO_INDEX=1"""

_FEATURES = "--features faster-tests,schemars"
_SUITES = [
    ("lib", f"cargo test --locked --no-fail-fast {_FEATURES} --lib"),
    (
        "integration",
        f"cargo test --locked --no-fail-fast {_FEATURES} --test run -- pgo:: integration::abi3_and_",
    ),
]
_SUITE_MARK = "maturin-suite"


def _test_commands() -> str:
    lines = ["rc=0"]
    for name, cmd in _SUITES:
        lines.append(f'echo "{_SUITE_MARK} {name}"')
        lines.append(f"{cmd} || rc=$?")
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _gate_commands() -> str:
    # Build every selected test target without running it (non-tolerant).
    return "\n".join(f"{cmd.split(' -- ')[0]} --no-run" for _, cmd in _SUITES)


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


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""


class MaturinImageBase(Image):
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
        # python3-dev/venv/pip + virtualenv: the integration tests build and
        # install wheels into venvs; the PGO flow uses `python -m venv`.
        return [
            "pkg-config",
            "python3-dev",
            "python3-venv",
            "python3-pip",
            "virtualenv",
        ]

    def extra_setup(self) -> str:
        # llvm-tools provides llvm-profdata for the PGO tests (rustup picks
        # the component for the build architecture).
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    RUST_BACKTRACE=1 \\\n"
            "    CI=true\n"
            "RUN rustup component add llvm-tools-preview \\\n"
            "    && git config --global --add safe.directory '*' \\\n"
            "    && rustc --version && cargo --version && python3 --version"
        )

    # dockerfile() is inherited: Image.dockerfile() emits FROM -> apt -> clone
    # -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening scrub.


class MaturinImageDefault(Image):
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
        return MaturinImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        tests = _test_commands()
        apply = _apply_flags(self.pr)
        fixtures = " ".join(_FIXTURES)
        packages = " ".join(_PY_PACKAGES)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

# Section 1 -- PIN
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2 -- PROVISION (network allowed here only)
retry() {{
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@"; then
      return 0
    fi
    sleep 20
  done
  return 1
}}
# Locked cargo deps of maturin and of every test-crates fixture (the unit
# tests run `cargo metadata` on fixtures; the integration tests build them).
retry cargo fetch --locked
while IFS= read -r lock; do
  dir="$(dirname "$lock")"
  status=network
  for attempt in 1 2 3 4 5; do
    if out="$(cargo fetch --locked --manifest-path "$dir/Cargo.toml" 2>&1)"; then
      status=ok
      break
    fi
    # A lockfile that is stale at this commit can never be fetched --locked;
    # anything else (network) is retried and then fatal.
    if grep -q "cannot update the lock file" <<<"$out"; then
      status=stale
      break
    fi
    sleep 20
  done
  echo "fixture fetch $dir: $status"
  test "$status" != network
done < <(find test-crates -name Cargo.lock -not -path '*/target*' | sort)
for fixture in {fixtures}; do
  cargo metadata --locked --offline --format-version 1 --manifest-path "$fixture/Cargo.toml" > /dev/null
done
# Wheelhouse for the wheel installs the tests perform (outside the repo).
mkdir -p {_WHEELHOUSE}
retry python3 -m pip download --dest {_WHEELHOUSE} {packages}

# Section 3 -- GATE (non-tolerant)
test "$(git rev-parse HEAD)" = "{sha}"
python3 -c "import ensurepip, sys; v = tuple(int(x) for x in ensurepip.version().split('.')[:2]); sys.exit(0 if v >= (25, 1) else 'ensurepip pip too old for --group')"
virtualenv --version
test -x "$(rustc --print sysroot)/lib/rustlib/$(rustc -vV | sed -n 's/^host: //p')/bin/llvm-profdata"
ls {_WHEELHOUSE}/attrs-*.whl {_WHEELHOUSE}/boltons-*.whl {_WHEELHOUSE}/cffi-*.whl
{_gate_commands()}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}
{_RUN_ENV}

cd /home/{repo}
{tests}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}
{_RUN_ENV}

cd /home/{repo}
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
{_RUN_ENV}

cd /home/{repo}
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

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("PyO3", "maturin")
class Maturin(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MaturinImageDefault(self.pr, self._config)

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
        """Names are `<binary>:<source> > <test>`, e.g.
        `maturin:src/lib.rs > pgo::tests::x` or `run:tests/run.rs > pgo::pgo_pyo3_mixed`.

        libtest names are only unique within one test binary, so each result
        is qualified by the preceding `Running` / `Doc-tests` line, with the
        per-build hash suffix stripped so names match across stages.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        suite_re = re.compile(rf"^{re.escape(_SUITE_MARK)} (\S+)$")
        running_re = re.compile(
            r"^Running (?:unittests )?(\S+) \((?:.*/)?([^/()]+?)(?:-[0-9a-f]{8,})?(?:\.exe)?\)$"
        )
        doctest_re = re.compile(r"^Doc-tests (\S+)$")
        result_re = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")
        doc_line = re.compile(r"\s+-\s+\(line \d+\)$|\s+\(line \d+\)$")

        target = ""
        for raw in log.splitlines():
            line = raw.strip()
            if suite_re.match(line):
                target = ""
                continue
            found = running_re.match(line)
            if found:
                target = f"{found.group(2)}:{found.group(1)}"
                continue
            found = doctest_re.match(line)
            if found:
                target = f"{found.group(1)}:doc"
                continue
            found = result_re.match(line)
            if not found or not target:
                continue
            name = f"{target} > {doc_line.sub('', found.group(1))}"
            status = found.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
