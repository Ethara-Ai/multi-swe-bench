from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.10-slim-bookworm"
_BASE_TAG = "base-4941_to_1466"
_RUSTUP_VERSION = "1.28.2"
_RUSTUP_SHA256_AMD64 = "20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c"
_RUSTUP_SHA256_ARM64 = "e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c"
_POETRY_VERSION = "1.8.3"
_SETUPTOOLS_VERSION = "65.5.0"
_WHEEL_VERSION = "0.38.4"
_VENV = "/home/venv"
_TEST_ENV = "/home/qdrant_test_env"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CRATE_RE = re.compile(r"^=== MSB_CRATE: (\S+) ===$")
_TARGET_RE = re.compile(r"^Running (?:unittests )?(\S+) \(")
_DOCTESTS_RE = re.compile(r"^Doc-tests\b")
_RESULT_RE = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)")
_DOC_NAME_RE = re.compile(r"^(\S+\.rs) - (.+)$")
_DOC_LINE_RE = re.compile(r"\s*\(line \d+\)")
_PY_ID = r"(?:openapi/tests|tests)/[^\s:]+\.py::.+?"
_PY_VERBOSE_RE = re.compile(
    rf"^({_PY_ID})\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_PY_SUMMARY_RE = re.compile(
    rf"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+({_PY_ID})(?:\s+-\s.*)?$"
)

_RUN_HEADER = f"""#!/bin/bash
set -eo pipefail

export CI=true
source {_TEST_ENV}"""

_PYTEST_ARGS = "-v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors"


def _test_cmds(repo: str) -> str:
    return f"""cd /home/{repo}
fatal=0

cargo build --offline --features "$QDRANT_FEATURES"

export QDRANT__SERVICE__GRPC_PORT=6334
./target/debug/qdrant > /tmp/qdrant-server.log 2>&1 &
server=$!
ready=0
for _ in $(seq 1 150); do
  curl --output /dev/null --silent --get --fail http://localhost:6333/collections && {{ ready=1; break; }}
  sleep 2
done
[ "$ready" = 1 ] || {{ echo "=== MSB: qdrant server did not start ==="; tail -50 /tmp/qdrant-server.log; fatal=1; }}

rc=0
python -m pytest "$QDRANT_OPENAPI_DIR" {_PYTEST_ARGS} --rootdir=/home/{repo} || rc=$?
[ "$rc" -le 1 ] || {{ echo "=== MSB: openapi pytest exited with status $rc ==="; fatal=$rc; }}
kill -9 "$server" 2>/dev/null || echo "qdrant server already stopped"
wait "$server" 2>/dev/null || echo "qdrant server stopped"
unset QDRANT__SERVICE__GRPC_PORT

rc=0
python -m pytest tests/consensus_tests {_PYTEST_ARGS} --rootdir=/home/{repo} || rc=$?
[ "$rc" -le 1 ] || {{ echo "=== MSB: consensus pytest exited with status $rc ==="; fatal=$rc; }}

cargo metadata --offline --no-deps --format-version 1 | python -c 'import json, os, sys; m = json.load(sys.stdin); [print(p["name"], os.path.relpath(os.path.dirname(p["manifest_path"]), m["workspace_root"])) for p in sorted(m["packages"], key=lambda p: p["name"]) if p["id"] in m["workspace_members"]]' > /tmp/qdrant-packages.txt
while read -r pkg dir; do
  echo "=== MSB_CRATE: $dir ==="
  rc=0
  cargo test --offline -p "$pkg" --no-fail-fast < /dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 101 ] || {{ echo "=== MSB: cargo test -p $pkg exited with status $rc ==="; fatal=$rc; }}
done < /tmp/qdrant-packages.txt

exit "$fatal"
"""


class Qdrant4941To1466ImageBase(Image):
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
    git ca-certificates curl build-essential pkg-config clang libclang-dev cmake \\
    protobuf-compiler libprotobuf-dev libssl-dev procps \\
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \\
    CARGO_HOME=/usr/local/cargo \\
    RUSTUP_AUTO_INSTALL=0 \\
    PATH=/usr/local/cargo/bin:$PATH

RUN set -eu; \\
    case "$(dpkg --print-architecture)" in \\
      amd64) triple=x86_64-unknown-linux-gnu; sum={_RUSTUP_SHA256_AMD64} ;; \\
      arm64) triple=aarch64-unknown-linux-gnu; sum={_RUSTUP_SHA256_ARM64} ;; \\
      *) echo "unsupported architecture $(dpkg --print-architecture)"; exit 1 ;; \\
    esac; \\
    curl --proto '=https' --tlsv1.2 -fsSL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/archive/{_RUSTUP_VERSION}/$triple/rustup-init"; \\
    echo "$sum  /tmp/rustup-init" | sha256sum -c -; \\
    chmod +x /tmp/rustup-init; \\
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none; \\
    rm -f /tmp/rustup-init; \\
    rustup set auto-self-update disable; \\
    rustup --version | grep -q "^rustup {_RUSTUP_VERSION} "

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Qdrant4941To1466ImageDefault(Image):
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
        return Qdrant4941To1466ImageBase(self.pr, self.config)

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

toolchain="$(sed -n 's/^FROM .*cargo-chef:latest-rust-\\([0-9][0-9.]*\\).*/\\1/p' Dockerfile | head -n 1)"
[ -n "$toolchain" ] || {{ echo "prepare.sh: no rust version in Dockerfile"; exit 1; }}
rustup toolchain install --profile minimal "$toolchain"
rustup default "$toolchain"
cargo --version | grep -q " $toolchain " || {{ echo "prepare.sh: toolchain $toolchain is not active"; exit 1; }}

features="$(sed -n 's/^ *run: cargo build --features "\\{{0,1\\}}\\([^"]*\\)"\\{{0,1\\}} *$/\\1/p' .github/workflows/integration-tests.yml | head -n 1)"
[ -n "$features" ] || {{ echo "prepare.sh: no cargo features in integration-tests.yml"; exit 1; }}
openapi_dir=openapi/tests/openapi_integration
[ -d "$openapi_dir" ] || openapi_dir=tests/openapi
[ -d "$openapi_dir" ] || {{ echo "prepare.sh: no openapi test directory"; exit 1; }}
printf 'export QDRANT_FEATURES="%s"\\nexport QDRANT_OPENAPI_DIR="%s"\\n' "$features" "$openapi_dir" > {_TEST_ENV}

/usr/local/bin/python -m venv {_VENV}
if [ -f openapi/tests/requirements-freeze.txt ]; then
  printf 'setuptools=={_SETUPTOOLS_VERSION}\\nwheel=={_WHEEL_VERSION}\\n' > /tmp/build-constraints.txt
  PIP_CONSTRAINT=/tmp/build-constraints.txt {_VENV}/bin/pip install --no-cache-dir --use-pep517 -r openapi/tests/requirements-freeze.txt
else
  /usr/local/bin/python -m venv /home/poetry
  /home/poetry/bin/pip install --no-cache-dir poetry=={_POETRY_VERSION}
  /home/poetry/bin/poetry -C tests check --lock
  POETRY_VIRTUALENVS_CREATE=false /home/poetry/bin/poetry -C tests install --no-root --no-interaction
fi
{_VENV}/bin/python -c "import sys, pytest, requests, schemathesis; assert sys.prefix == '{_VENV}', sys.prefix" || {{ echo "prepare.sh: python test dependencies are not importable from {_VENV}"; exit 1; }}

cargo fetch --locked
cargo build --offline --locked --features "$features"
test -x target/debug/qdrant || {{ echo "prepare.sh: target/debug/qdrant was not built"; exit 1; }}
cargo metadata --offline --no-deps --format-version 1 | python -c 'import json, os, sys; m = json.load(sys.stdin); [print(p["name"], os.path.relpath(os.path.dirname(p["manifest_path"]), m["workspace_root"])) for p in sorted(m["packages"], key=lambda p: p["name"]) if p["id"] in m["workspace_members"]]' > /tmp/qdrant-packages.txt
[ -s /tmp/qdrant-packages.txt ] || {{ echo "prepare.sh: no workspace packages found"; exit 1; }}
while read -r pkg dir; do
  cargo test --offline -p "$pkg" --no-run < /dev/null || {{ echo "prepare.sh: cargo test --no-run failed for $pkg"; exit 1; }}
done < /tmp/qdrant-packages.txt

QDRANT__SERVICE__GRPC_PORT=6334 ./target/debug/qdrant > /tmp/qdrant-smoke.log 2>&1 &
server=$!
ready=0
for _ in $(seq 1 150); do
  curl --output /dev/null --silent --get --fail http://localhost:6333/collections && {{ ready=1; break; }}
  sleep 2
done
kill -9 "$server" 2>/dev/null || echo "qdrant server already stopped"
wait "$server" 2>/dev/null || echo "qdrant server stopped"
rm -rf storage snapshots
[ "$ready" = 1 ] || {{ tail -50 /tmp/qdrant-smoke.log; echo "prepare.sh: qdrant server did not start"; exit 1; }}

{_VENV}/bin/python -m pytest "$openapi_dir" tests/consensus_tests --collect-only -q -p no:cacheprovider --rootdir=/home/{self.pr.repo} > /tmp/collect.log || {{ tail -30 /tmp/collect.log; echo "prepare.sh: pytest cannot collect the integration suites"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_test_cmds(self.pr.repo)}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("Qdrant4941To1466ImageDefault dependency must be an Image")
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
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=gcc \\
    RUSTFLAGS="-C debuginfo=0" \\
    RUST_BACKTRACE=1 \\
    OPENAPI_FILE=/home/{self.pr.repo}/docs/redoc/master/openapi.json \\
    PATH={_VENV}/bin:$PATH

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("qdrant", "qdrant_4941_to_1466")
class QDRANT_4941_TO_1466(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Qdrant4941To1466ImageDefault(self.pr, self._config)

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
        rust_buckets = {"ok": passed, "FAILED": failed, "ignored": skipped}
        py_buckets = {
            "PASSED": passed,
            "FAILED": failed,
            "ERROR": failed,
            "SKIPPED": skipped,
            "XFAIL": skipped,
            "XPASS": skipped,
        }

        crate = ""
        target = ""
        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()

            py = _PY_VERBOSE_RE.match(line)
            if py:
                py_buckets[py.group(2)].add(py.group(1))
                continue
            py = _PY_SUMMARY_RE.match(line)
            if py:
                py_buckets[py.group(1)].add(py.group(2))
                continue

            marker = _CRATE_RE.match(line)
            if marker:
                crate = "" if marker.group(1) == "." else marker.group(1)
                target = ""
                continue

            running = _TARGET_RE.match(line)
            if running:
                target = running.group(1)
                continue

            if _DOCTESTS_RE.match(line):
                target = ""
                continue

            result = _RESULT_RE.match(line)
            if not result:
                continue

            name, status = result.group(1), result.group(2)
            doc = _DOC_NAME_RE.match(name)
            if doc:
                path, name = doc.group(1), _DOC_LINE_RE.sub("", doc.group(2))
            else:
                path = target
            if crate and path and not path.startswith(f"{crate}/"):
                path = f"{crate}/{path}"
            rust_buckets[status].add(f"{path}::{name}" if path else name)

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
