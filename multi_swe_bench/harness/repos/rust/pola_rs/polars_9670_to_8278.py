from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.11-slim-bookworm"
_BASE_TAG = "base-9670_to_8278"
_RUSTUP_VERSION = "1.28.2"
_RUSTUP_SHA256_AMD64 = "20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c"
_RUSTUP_SHA256_ARM64 = "e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c"
_UV_VERSION = "0.8.17"
_VENV = "/home/venv"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_ID = r"tests/[^\s:]+\.py::.+?"
_VERBOSE_RE = re.compile(
    rf"^({_TEST_ID})\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_SUMMARY_RE = re.compile(
    rf"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+({_TEST_ID})(?:\s+-\s.*)?$"
)
_ID_PREFIX = "py-polars/"

_RUN_HEADER = f"""#!/bin/bash
set -eo pipefail

export CI=true
export VIRTUAL_ENV={_VENV}
export PATH={_VENV}/bin:$PATH"""

_TEST_CMDS = """cd /home/polars/py-polars
cargo build --offline --lib
cp target/debug/libpolars.so polars/polars.abi3.so
rc=0
python -m pytest tests -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors || rc=$?
if [ "$rc" -gt 1 ]; then
  echo "pytest exited with status $rc" >&2
  exit "$rc"
fi"""


class Polars9670To8278ImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv=={_UV_VERSION} && uv --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Polars9670To8278ImageDefault(Image):
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
        return Polars9670To8278ImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

case "$(dpkg --print-architecture)" in
  amd64) triple=x86_64-unknown-linux-gnu; sum={_RUSTUP_SHA256_AMD64} ;;
  arm64) triple=aarch64-unknown-linux-gnu; sum={_RUSTUP_SHA256_ARM64} ;;
  *) echo "prepare.sh: unsupported architecture $(dpkg --print-architecture)"; exit 1 ;;
esac
curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/$triple/rustup-init"
echo "$sum  /tmp/rustup-init" | sha256sum -c -
chmod +x /tmp/rustup-init
/tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none
rm -f /tmp/rustup-init
rustup --version

toolchain="$(sed -n 's/^channel = "\\(.*\\)"$/\\1/p' rust-toolchain.toml)"
[ -n "$toolchain" ] || {{ echo "prepare.sh: no channel in rust-toolchain.toml"; exit 1; }}
rustup toolchain install --profile minimal "$toolchain"
rustup toolchain list | grep -q "^$toolchain-" || {{ echo "prepare.sh: toolchain $toolchain did not install"; exit 1; }}

cutoff="$(date -u -d "@$(git show -s --format=%ct HEAD)" +%Y-%m-%dT%H:%M:%SZ)"
python -m venv {_VENV}
grep -v '^connectorx' py-polars/requirements-dev.txt > /tmp/requirements-dev.txt
uv pip install --no-cache --compile-bytecode --python {_VENV}/bin/python --exclude-newer "$cutoff" --only-binary :all: -r /tmp/requirements-dev.txt

cd /home/{self.pr.repo}/py-polars
cargo fetch
cargo build --offline --lib
cp target/debug/libpolars.so polars/polars.abi3.so
{_VENV}/bin/python -m compileall -q polars
git checkout -- Cargo.lock
echo /home/{self.pr.repo}/py-polars > "$({_VENV}/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/polars-source-tree.pth"

cd /tmp
{_VENV}/bin/python -c "import polars, pytest, hypothesis, numpy, pandas, pyarrow, deltalake, xlsx2csv, xlsxwriter; assert polars.__file__ == '/home/{self.pr.repo}/py-polars/polars/__init__.py', polars.__file__; print('polars', polars.__version__)" || {{ echo "prepare.sh: polars or a test dependency does not import from the source tree"; exit 1; }}
cd /home/{self.pr.repo}/py-polars
{_VENV}/bin/python -m pytest tests --collect-only -q -p no:cacheprovider > /tmp/collect.log || {{ tail -30 /tmp/collect.log; echo "prepare.sh: pytest cannot collect the test suite"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Polars9670To8278ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    VIRTUAL_ENV={_VENV} \\
    RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_AUTO_INSTALL=0 \\
    RUSTFLAGS="-C debuginfo=0" \\
    RUST_BACKTRACE=1 \\
    PATH={_VENV}/bin:/usr/local/cargo/bin:$PATH

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("pola-rs", "polars_9670_to_8278")
class POLARS_9670_TO_8278(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Polars9670To8278ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            name = status = None
            m = _VERBOSE_RE.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name or not status:
                continue
            name = _ID_PREFIX + name
            if status == "PASSED":
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
