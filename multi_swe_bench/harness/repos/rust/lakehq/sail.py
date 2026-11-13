"""lakehq/sail -- Rust workspace (DataFusion/Arrow) shipped as the `pysail`
Python package (maturin, `crates/sail-python` cdylib -> `pysail._native`).

Per-PR base (`base-pr-<N>`): the inherited Image.dockerfile() clones, pins
${BASE_COMMIT} and runs the hardening scrub; DockerfileEnhancer adds the
BuildKit directive, ARGs, proxy/TLS env and cert links.

Toolchain: Cargo.toml `rust-version = "1.96.0"`, no rust-toolchain file;
docker/release/Dockerfile pins RUST_VERSION=1.96.0 (rustup profile default,
which includes rustfmt: sail-common-hms's build.rs formats generated code) and installs
protobuf-compiler + libprotobuf-dev (build.rs of sail-common-datafusion,
sail-execution and sail-spark-connect run protoc). The official
`rust:1.96.0-bookworm` image is multi-arch; bookworm's python3 is 3.11, the
version CI tests with.

Test scope: the PR's test patch touches Iceberg tests. The REST-catalog and
Nessie tests (`catalog/iceberg_rest/`) are integration tests that need
SeaweedFS/Iceberg-REST/Nessie containers and are left out. The selected
modules run against an in-process Sail Spark Connect server and a local
pyiceberg SQLite catalog, fully offline:
  * python/pysail/tests/spark/iceberg/test_iceberg_write.py
  * python/pysail/tests/spark/iceberg/test_iceberg_features.py (pytest-bdd
    scenarios from iceberg/features/*.feature)

Build: like CI (maturin-action `build`, dev profile), the extension is built
in the dev profile, here with debug info off and incremental off to keep the
target dir small. prepare.sh builds it once (`maturin develop`, editable
install into a venv at /opt/sail-venv, outside the repo); each run script
re-runs `maturin develop --skip-install` offline so the fix stage recompiles
the patched crates. The extension lands in python/pysail/ as `*.so` and the
cargo output in `target/` -- both git-ignored, so they survive
`git reset --hard` + `git clean -fd`. Nothing in prepare modifies tracked
files.
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.96.0-bookworm"
_VENV = "/opt/sail-venv"

_TEST_PATHS = [
    "python/pysail/tests/spark/iceberg/test_iceberg_write.py",
    "python/pysail/tests/spark/iceberg/test_iceberg_features.py",
]

_ENV = f"""export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS=3
export CARGO_PROFILE_DEV_DEBUG=0
export RUST_BACKTRACE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1
export VIRTUAL_ENV={_VENV}
export PATH={_VENV}/bin:$PATH
# rust-lld (default x86_64 linker since 1.90) segfaults under QEMU amd64 emulation; use the system linker
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS='-C linker-features=-lld'"""

_RUN_ENV = """export CARGO_NET_OFFLINE=true
export PIP_NO_INDEX=1"""

_BUILD = "maturin develop --locked --skip-install"
_PYTEST = (
    "python -m pytest -p no:cacheprovider -rA -v --color=no "
    + " ".join(_TEST_PATHS)
)


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

# Reads the `test` extra of pysail from pyproject.toml at the checked-out
# commit, so the Python test deps follow the PR's own pins.
_TEST_DEPS_PY = """import sys
import tomllib

with open("pyproject.toml", "rb") as f:
    data = tomllib.load(f)
deps = data["project"]["optional-dependencies"]["test"]
build = data["build-system"]["requires"]
with open(sys.argv[1], "w") as out:
    for dep in build + deps:
        # pyspark-client lacks the Spark distribution, and pyspark.testing
        # exits at import without a SPARK_HOME. Upstream CI (python-tests.yml,
        # default hatch env) uses the full pyspark[connect] at the same version.
        if dep.replace(" ", "").startswith("pyspark-client=="):
            dep = "pyspark[connect]==" + dep.split("==", 1)[1].strip()
        out.write(dep + "\\n")
    # pytest 9.2 removed FixtureManager._arg2fixturedefs, which pytest-bdd 8.1
    # still uses; 9.1.1 stays inside upstream's "pytest>=9.1,<10" range.
    out.write("pytest==9.1.1\\n")
"""


class SailImageBase(Image):
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
        return [
            "pkg-config",
            "cmake",
            "protobuf-compiler",
            "libprotobuf-dev",
            "python3-dev",
            "python3-venv",
            "python3-pip",
        ]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    RUST_BACKTRACE=1 \\\n"
            "    CI=true\n"
            "RUN rustup component add rustfmt \\\n"
            "    && git config --global --add safe.directory '*' \\\n"
            "    && rustc --version && cargo --version && python3 --version && protoc --version"
        )

    # dockerfile() is inherited: Image.dockerfile() emits FROM -> apt -> clone
    # -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening scrub.


class SailImageDefault(Image):
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
        return SailImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
        tests = " ".join(_TEST_PATHS)
        run_body = f"""{_BUILD}
python -c "import pysail._native"
{_PYTEST}"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "sail_test_deps.py", _TEST_DEPS_PY),
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
python3 -m venv {_VENV}
retry python -m pip install --upgrade pip
python /home/sail_test_deps.py /opt/sail-test-requirements.txt
cat /opt/sail-test-requirements.txt
retry python -m pip install -r /opt/sail-test-requirements.txt
retry cargo fetch --locked
# Build pysail._native (dev profile) and install pysail editable into the venv.
CARGO_NET_OFFLINE=true maturin develop --locked

# Section 3 -- GATE (non-tolerant)
test "$(git rev-parse HEAD)" = "{sha}"
python -c "import pysail._native, pyspark.sql.connect, pyiceberg, pytest_bdd; from pysail.spark import SparkConnectServer"
CARGO_NET_OFFLINE=true {_BUILD}
python -m pytest -p no:cacheprovider --collect-only -q {tests}
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
{run_body}
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
{run_body}
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
{run_body}
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


@Instance.register("lakehq", "sail")
class Sail(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SailImageDefault(self.pr, self._config)

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
        """Parses the pytest `-rA` short test summary.

        Names are pytest node ids, e.g.
        `python/pysail/tests/spark/iceberg/test_iceberg_write.py::test_x`.
        Only summary lines are read, so the trailing ` - <reason>` on
        FAILED/ERROR/SKIPPED lines is stripped and names are stable across
        stages.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        in_summary = False
        summary_re = re.compile(r"^=+ short test summary info =+$")
        result_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+?::\S.*?)(?: - .*)?$"
        )
        skip_re = re.compile(r"^SKIPPED \[\d+\] (\S+?):\d+: ")
        for raw in log.splitlines():
            line = raw.rstrip()
            if summary_re.match(line):
                in_summary = True
                continue
            if not in_summary:
                continue
            if re.match(r"^=+ .* in [\d.]+s.* =+$", line):
                in_summary = False
                continue
            found = result_re.match(line)
            if found:
                status, name = found.group(1), found.group(2).strip()
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            found = skip_re.match(line)
            if found:
                # `-rA` reports skips by location only; keep them as a file
                # level skip marker (never credited, never collides with a
                # node id).
                skipped_tests.add(f"{found.group(1)} [skipped]")

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
