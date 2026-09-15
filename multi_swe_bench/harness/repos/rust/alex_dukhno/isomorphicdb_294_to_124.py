import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_TOOLCHAIN = "1.46.0"

PIP_PINS = " ".join(
    [
        "pytest==5.4.3",
        "psycopg2-binary==2.8.5",
        "attrs==19.3.0",
        "importlib-metadata==1.7.0",
        "more-itertools==8.4.0",
        "packaging==20.4",
        "pluggy==0.13.1",
        "py==1.9.0",
        "pyparsing==2.4.7",
        "six==1.15.0",
        "wcwidth==0.2.5",
        "zipp==3.1.0",
    ]
)

TEST_COMMAND = """cd /home/[[REPO]]
if cargo test --lib --all --no-fail-fast --locked 2>&1; then
    unit_rc=0
else
    unit_rc=$?
fi
if cargo build --bin database --locked 2>&1; then
    build_rc=0
else
    build_rc=$?
fi
func_rc=$build_rc
if [ "$build_rc" -eq 0 ]; then
    target/debug/database > /tmp/isomorphicdb-server.log 2>&1 &
    server_pid=$!
    for i in $(seq 1 60); do
        if awk 'NR>1 && $2 ~ /:1538$/ && $4=="0A"' /proc/net/tcp /proc/net/tcp6 2>/dev/null | grep -q .; then
            break
        fi
        sleep 1
    done
    if python3 -m pytest -v -p no:cacheprovider tests/functional/*.py 2>&1; then
        func_rc=0
    else
        func_rc=$?
    fi
    if kill -0 "$server_pid" 2>/dev/null; then
        kill "$server_pid"
    fi
fi
echo "STAGE_EXIT_CODES unit=$unit_rc build=$build_rc functional=$func_rc"
if [ "$unit_rc" -ne 0 ]; then
    exit "$unit_rc"
fi
if [ "$build_rc" -ne 0 ]; then
    exit "$build_rc"
fi
exit "$func_rc"
"""

RUN_HEADER = """#!/bin/bash
set -eo pipefail
export CI=true
export RUSTUP_TOOLCHAIN=[[RUST_TOOLCHAIN]]
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1
""".replace("[[RUST_TOOLCHAIN]]", RUST_TOOLCHAIN)


class ImageBase(Image):
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
        return f"rust:{RUST_TOOLCHAIN}"

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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

{self.global_env}

ENV RUSTUP_TOOLCHAIN={RUST_TOOLCHAIN} \\
    CARGO_NET_GIT_FETCH_WITH_CLI=true \\
    CARGO_TERM_COLOR=never

WORKDIR /home/

RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends \\
        git ca-certificates python3 python3-pip python3-setuptools python3-dev gcc libpq-dev && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN USER=root cargo new --vcs none /tmp/crates-index-warmup && \\
    cd /tmp/crates-index-warmup && \\
    printf 'libc = "=0.2.72"\\n' >> Cargo.toml && \\
    cargo fetch && \\
    rm -rf /tmp/crates-index-warmup

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        test_command = TEST_COMMAND.replace("[[REPO]]", repo)

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
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUSTUP_TOOLCHAIN=[[RUST_TOOLCHAIN]]
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_TERM_COLOR=never
export PIP_DISABLE_PIP_VERSION_CHECK=1

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
git clean -fdx
bash /home/check_git_changes.sh

cargo fetch --locked
cargo test --lib --all --no-run --locked
cargo build --bin database --locked
printf '%s\\n' [[PIP_PINS]] > /tmp/functional-constraints.txt
python3 -m pip install --no-cache-dir -r tests/functional/requirements.txt -c /tmp/functional-constraints.txt

rustc --version | grep -F "rustc [[RUST_TOOLCHAIN]]"
cargo --version | grep -F "cargo [[RUST_TOOLCHAIN]]"
test -x target/debug/database
python3 -c "import psycopg2, pytest; assert pytest.__version__ == '5.4.3', pytest.__version__; print('psycopg2', psycopg2.__version__, 'pytest', pytest.__version__)"
python3 -m pytest --collect-only -q -p no:cacheprovider tests/functional/*.py > /tmp/collect-gate.log
test "$(grep -c '::test_' /tmp/collect-gate.log)" -gt 0
echo DEPS_OK
""".replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
                .replace("[[PIP_PINS]]", PIP_PINS)
                .replace("[[RUST_TOOLCHAIN]]", RUST_TOOLCHAIN),
            ),
            File(
                ".",
                "run.sh",
                RUN_HEADER
                + """
[[TEST_COMMAND]]
""".replace("[[TEST_COMMAND]]", test_command),
            ),
            File(
                ".",
                "test-run.sh",
                RUN_HEADER
                + """
cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "PATCH_APPLY_FAILED: test.patch does not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                RUN_HEADER
                + """
cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "PATCH_APPLY_FAILED: test.patch + fix.patch do not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("alex-dukhno", "isomorphicdb")
class AlexDukhnoIsomorphicdb(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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
        seen: dict[str, int] = {}
        last_base = ""
        last_name = ""

        cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        running_re = re.compile(r"^\s*Running\s+.*?/deps/(?P<crate>[A-Za-z0-9_]+)-[0-9a-f]+\)?\s*$")
        rust_re = re.compile(r"^test (?P<name>\S+) \.\.\. (?P<status>ok|FAILED|ignored)\s*$")
        py_re = re.compile(
            r"^(?P<name>tests/\S+?\.py::.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
        )

        crate = ""
        for line in cleaned.splitlines():
            line = line.rstrip()
            m = running_re.match(line)
            if m:
                crate = m.group("crate")
                continue
            m = rust_re.match(line)
            if m:
                base_name = f"{crate}::{m.group('name')}"
                status = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}[m.group("status")]
            else:
                m = py_re.match(line)
                if not m:
                    continue
                base_name = m.group("name")
                status = m.group("status")
                if status == "ERROR" and base_name == last_base:
                    failed_tests.add(last_name)
                    continue
            n = seen.get(base_name, 0) + 1
            seen[base_name] = n
            name = base_name if n == 1 else f"{base_name} #{n}"
            last_base = base_name
            last_name = name
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
