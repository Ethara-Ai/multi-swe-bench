"""lancedb/lancedb -- Rust core (`rust/lancedb`) with Python bindings
(`python/`, a maturin/pyo3 cdylib `lancedb._lancedb`) and Node bindings.

The PRs in this set are Python-facing: their test patches edit
`python/python/tests/*.py` (pytest, driven against an in-process mock HTTP
server on 127.0.0.1 -- no cloud services, no external network), while their
fix patches touch both the Python package and the Rust crate
(`rust/lancedb/src/remote/...`). So every stage rebuilds the native extension
from the current tree before running pytest.

Per-PR base (`base-pr-<N>`): the inherited Image.dockerfile() clones, pins
${BASE_COMMIT} and runs the hardening scrub; DockerfileEnhancer adds the
BuildKit directive, ARGs, proxy/TLS env and cert links.

Toolchain: `rust-toolchain.toml` pins channel 1.97.0 at both base shas
(workspace `rust-version = "1.94.0"`), so the image is the official multi-arch
`rust:1.97.0-bookworm`. protoc comes from apt `protobuf-compiler` (CI), plus `libprotobuf-dev` for
the well-known `google/protobuf/*.proto` includes lance-encoding imports.
Python is bookworm's 3.11 (`requires-python >= 3.10`).

Python deps come from the repo's own `python/uv.lock` (`uv sync --frozen
--extra tests`, which also resolves `pylance` from the fury index the lock
names) into a venv outside the repo (/opt/lancedb-venv). maturin is installed
at the version the lock pins, into a separate tools venv.

Native build scope (disk/CPU):
`--no-default-features --features remote,lancedb/metrics-otel`.
The python crate's default features add the aws/gcs/azure/dynamodb/oss/cos/
goosefs/huggingface object stores, none of which the selected tests use;
`remote` gates the remote client the tests exercise, and `metrics-otel` is
required because the bindings reference `lancedb::metrics_otel`
unconditionally. Profile `ci` (the repo's own: dev, no incremental, no debug
info on dependencies), with debug info also off for the workspace crates
(CARGO_PROFILE_CI_DEBUG=0) to keep the image small.

Offline guarantees: prepare.sh builds the extension once (fetching all cargo
deps, incl. the lance git deps, into CARGO_HOME). The run scripts set
CARGO_NET_OFFLINE / UV_OFFLINE / PIP_NO_INDEX and rebuild in place with
`maturin develop --skip-install` (no install step, no network); after a
`git reset --hard` + `git clean -fd` the git-ignored `target/` and the
extension `.so` survive and cargo only recompiles the workspace crates.

Test scope: the test files the PR's test patch touches (existing ones only,
so the baseline stage still runs), with CI's marker filter
`-m "not slow and not s3_test"`.
"""

import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.97.0-bookworm"

_VENV = "/opt/lancedb-venv"
_TOOLS = "/opt/lancedb-tools"
_UV_VERSION = "0.9.5"
_FURY = "https://pypi.fury.io/lance-format/"
_FURY_DB = "https://pypi.fury.io/lancedb/"

_ENV = f"""export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_CI_DEBUG=0
export CARGO_PROFILE_CI_STRIP=debuginfo
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS="${{CARGO_BUILD_JOBS:-3}}"
export RUST_BACKTRACE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_PYTHON_DOWNLOADS=never
export UV_LINK_MODE=copy
export UV_PROJECT_ENVIRONMENT={_VENV}
export VIRTUAL_ENV={_VENV}
export PATH={_VENV}/bin:{_TOOLS}/bin:$PATH"""

_RUN_ENV = """export CARGO_NET_OFFLINE=true
export UV_OFFLINE=1
export PIP_NO_INDEX=1"""

_BUILD = (
    "maturin develop --skip-install --locked --profile ci "
    "--no-default-features --features remote,lancedb/metrics-otel"
)

_PYTEST = (
    'python -m pytest -v -rfE -p no:cacheprovider -m "not slow and not s3_test"'
)


def _test_files(pr: PullRequest) -> list[str]:
    """pytest files (relative to `python/`) touched by the test patch."""
    files: list[str] = []
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)", pr.test_patch, re.M):
        if re.match(r"^python/python/tests/(?:.*/)?test_[^/]*\.py$", path):
            rel = path[len("python/") :]
            if rel not in files:
                files.append(rel)
    return files


def _test_commands(pr: PullRequest) -> str:
    files = " ".join(shlex.quote(f) for f in _test_files(pr))
    return f"""cd /home/{pr.repo}/python
{_BUILD}
files=()
for f in {files}; do
  if [ -f "$f" ]; then files+=("$f"); fi
done
test "${{#files[@]}}" -gt 0
{_PYTEST} "${{files[@]}}\""""


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


class LancedbImageBase(Image):
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
            "libssl-dev",
            "protobuf-compiler",
            "libprotobuf-dev",
            "python3-dev",
            "python3-venv",
        ]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    RUST_BACKTRACE=1 \\\n"
            "    CI=true\n"
            "RUN git config --global --add safe.directory '*' \\\n"
            "    && rustc --version && cargo --version \\\n"
            "    && protoc --version && python3 --version"
        )

    # dockerfile() is inherited: Image.dockerfile() emits FROM -> apt -> clone
    # -> reset -> checkout ${BASE_COMMIT} -> extra_setup -> hardening scrub.


class LancedbImageDefault(Image):
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
        return LancedbImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        tests = _test_commands(self.pr)
        apply = _apply_flags(self.pr)
        files = " ".join(shlex.quote(f) for f in _test_files(self.pr))
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
cd /home/{repo}/python
maturin_version="$(python3 -c 'import tomllib; d = tomllib.load(open("uv.lock", "rb")); print(next(p["version"] for p in d["package"] if p["name"] == "maturin"))')"
python3 -m venv {_TOOLS}
retry {_TOOLS}/bin/pip install "uv=={_UV_VERSION}" "maturin==$maturin_version"
# Era-locked Python deps from the repo's uv.lock (the project itself is
# installed below by maturin as an editable package).
retry uv sync --frozen --no-dev --no-install-project --extra tests --python /usr/bin/python3
uv cache clean
# Native extension + editable install (fetches every cargo dep, incl. the
# lance git deps, into CARGO_HOME; build outputs land in git-ignored target/).
maturin develop --uv --locked --profile ci --no-default-features --features remote,lancedb/metrics-otel

# Section 3 -- GATE (non-tolerant)
cd /home/{repo}
test "$(git rev-parse HEAD)" = "{sha}"
cd /home/{repo}/python
python -c "import lancedb, lancedb._lancedb, lancedb.remote, lance, pytest_asyncio; print(lancedb.__file__)"
python -c "import lancedb, os, sys; sys.exit(0 if lancedb.__file__.startswith(os.getcwd()) else 'lancedb is not the editable repo package')"
CARGO_NET_OFFLINE=true {_BUILD}
python -m pytest --collect-only -q -p no:cacheprovider {files} > /dev/null
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


@Instance.register("lancedb", "lancedb")
class Lancedb(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LancedbImageDefault(self.pr, self._config)

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
        """pytest `-v` result lines: `<file>::<test>[<params>] STATUS [ NN%]`.

        Names are pytest node ids relative to `python/` (e.g.
        `python/tests/test_remote_db.py::test_http_error`); the trailing
        progress percentage is not captured. Collection errors (`ERROR
        <file>`, from `-rfE`) are reported as failures of that file.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        result_re = re.compile(
            r"^(\S+\.py::\S.*?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$"
        )
        collect_re = re.compile(r"^ERROR (\S+\.py)(?: - .*)?$")

        for raw in log.splitlines():
            line = raw.rstrip()
            found = result_re.match(line)
            if found:
                name, status = found.group(1), found.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            found = collect_re.match(line)
            if found:
                failed_tests.add(found.group(2))

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
