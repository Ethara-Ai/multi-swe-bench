import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.99.1-bookworm"
BASE_TAG = "base-9531_to_11118"
UV_VERSION = "0.9.17"
TEST_PYTHON = "3.13"
TEST_VENV = "/opt/qdrant-tests-venv"
SERVER_FEATURES = "service_debug data-consistency-check staging"

_ENV = f"""\
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_PROFILE_DEV_DEBUG=0
export RUSTUP_MAX_RETRIES=10
export RUST_BACKTRACE=1
export UV_PROJECT_ENVIRONMENT={TEST_VENV}
export UV_PYTHON={TEST_PYTHON}
export UV_HTTP_RETRIES=5
export QDRANT__SERVICE__GRPC_PORT=6334
"""


def _patch_paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)", patch, re.M)


def _rust_suites(test_patch: str) -> list[tuple[str, str, str, list[str]]]:
    suites: dict[tuple[str, str, str], set[str]] = {}
    for path in _patch_paths(test_patch):
        if not path.endswith(".rs"):
            continue
        parts = path.split("/")
        if "src" in parts:
            i = parts.index("src")
            crate = "/".join(parts[:i]) or "."
            mods = parts[i + 1 :]
            mods[-1] = mods[-1][:-3]
            if mods[-1] == "mod":
                mods = mods[:-1]
            filters = suites.setdefault((crate, "src", ""), set())
            if mods and not (len(mods) == 1 and mods[0] in ("lib", "main")):
                filters.add("::".join(mods) + "::")
        elif "tests" in parts:
            i = parts.index("tests")
            crate = "/".join(parts[:i]) or "."
            rest = parts[i + 1 :]
            if len(rest) == 1:
                suites.setdefault((crate, "test", rest[0][:-3]), set())
            else:
                filters = suites.setdefault((crate, "test", rest[0]), set())
                leaf = rest[-1]
                if leaf not in ("main.rs", "mod.rs"):
                    filters.add(f"{leaf[:-3]}::")
                elif leaf == "mod.rs" and len(rest) >= 3:
                    filters.add(f"{rest[-2]}::")
    return [
        (crate, kind, target, sorted(filters))
        for (crate, kind, target), filters in sorted(suites.items())
    ]


def _py_files(test_patch: str) -> tuple[list[str], list[str]]:
    server, standalone = [], []
    for path in _patch_paths(test_patch):
        parts = path.split("/")
        if parts[0] != "tests" or not path.endswith(".py"):
            continue
        if not parts[-1].startswith("test_"):
            continue
        (server if "openapi" in parts else standalone).append(path)
    return sorted(set(server)), sorted(set(standalone))


def _quoted(items: list[str]) -> str:
    return " ".join(f"'{i}'" for i in items)


def _manifest(crate: str) -> str:
    return "Cargo.toml" if crate == "." else f"{crate}/Cargo.toml"


def _rust_variants(crate: str, kind: str, target: str) -> list[tuple[str, str, str]]:
    if kind == "src":
        return [
            (f"[ -f '{crate}/src/lib.rs' ]", "--lib", f"{crate}:lib"),
            (f"[ -f '{crate}/src/main.rs' ]", "--bins", f"{crate}:bins"),
        ]
    return [
        (
            f"{{ [ -f '{crate}/tests/{target}/main.rs' ] || "
            f"[ -f '{crate}/tests/{target}.rs' ]; }}",
            f"--test '{target}'",
            f"{crate}:test:{target}",
        )
    ]


def _rust_block(crate: str, kind: str, target: str, body) -> str:
    branches = []
    for i, (guard, flag, suite) in enumerate(_rust_variants(crate, kind, target)):
        keyword = "if" if i == 0 else "elif"
        branches.append(f"{keyword} {guard}; then\n{body(flag, suite)}")
    return "\n".join(branches) + "\nfi"


def _py_collect(var: str, files: list[str]) -> str:
    return (
        f"{var}=()\n"
        f"for f in {_quoted(files)}; do\n"
        f'  if [ -f "$f" ]; then {var}+=("$f"); fi\n'
        f"done"
    )


_PYTEST = f"{TEST_VENV}/bin/python -m pytest -v -p no:cacheprovider"

_SERVER_BUILD = f"cargo build --bin qdrant --features '{SERVER_FEATURES}'"


def _gate_cmd(test_patch: str) -> str:
    lines = []
    for crate, kind, target, _ in _rust_suites(test_patch):
        lines.append(
            _rust_block(
                crate,
                kind,
                target,
                lambda flag, suite, crate=crate: (
                    f"  cargo test --manifest-path '{_manifest(crate)}' {flag} --no-run"
                ),
            )
        )
    server, standalone = _py_files(test_patch)
    if server or standalone:
        lines.append(_SERVER_BUILD)
        lines.append("test -x target/debug/qdrant")
        lines.append("uv sync --project tests --locked")
        lines.append(
            f"{TEST_VENV}/bin/python -c 'import pytest, requests, qdrant_client, grpc'"
        )
    lines.append("echo DEPS_OK")
    return "\n".join(lines)


def _test_cmd(test_patch: str) -> str:
    blocks = []
    for crate, kind, target, filters in _rust_suites(test_patch):
        args = f" -- {_quoted(filters)}" if filters else ""
        blocks.append(
            _rust_block(
                crate,
                kind,
                target,
                lambda flag, suite, crate=crate, args=args: (
                    f"  echo 'MSB_SUITE|{suite}'\n"
                    f"  cargo test --manifest-path '{_manifest(crate)}' {flag} --no-fail-fast{args} || RC=$?"
                ),
            )
        )
    server, standalone = _py_files(test_patch)
    if server or standalone:
        blocks.append(f"{_SERVER_BUILD} || RC=$?")
    if server:
        blocks.append(
            f"""\
{_py_collect("SERVER_FILES", server)}
if [ "${{#SERVER_FILES[@]}}" -gt 0 ]; then
  ./target/debug/qdrant > /tmp/qdrant-server.log 2>&1 &
  QDRANT_PID=$!
  READY=0
  for i in $(seq 1 90); do
    if curl --output /dev/null --silent --fail http://localhost:6333/collections; then READY=1; break; fi
    if ! kill -0 "$QDRANT_PID" 2>/dev/null; then break; fi
    sleep 2
  done
  if [ "$READY" = 1 ]; then
    echo 'MSB_SUITE|pytest:server'
    {_PYTEST} "${{SERVER_FILES[@]}}" || RC=$?
  else
    tail -n 50 /tmp/qdrant-server.log
    RC=1
  fi
  kill -9 "$QDRANT_PID" 2>/dev/null || true
  wait "$QDRANT_PID" 2>/dev/null || true
fi"""
        )
    if standalone:
        blocks.append(
            f"""\
{_py_collect("STANDALONE_FILES", standalone)}
if [ "${{#STANDALONE_FILES[@]}}" -gt 0 ]; then
  echo 'MSB_SUITE|pytest:standalone'
  {_PYTEST} "${{STANDALONE_FILES[@]}}" || RC=$?
fi"""
        )
    body = "\n\n".join(blocks)
    return f"""\
RC=0

{body}

exit $RC"""


_CHECK_GIT_CHANGES = """\
#!/bin/bash
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


class Qdrant9531To11118ImageBase(Image):
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
        return RUST_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_NET_RETRY=10 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    RUSTUP_MAX_RETRIES=10 \\
    RUST_BACKTRACE=1 \\
    UV_PROJECT_ENVIRONMENT={TEST_VENV} \\
    UV_PYTHON={TEST_PYTHON} \\
    UV_HTTP_RETRIES=5 \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential clang lld cmake pkg-config \\
        protobuf-compiler libprotobuf-dev libunwind-dev curl jq python3 python3-venv xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/uv && \\
    /opt/uv/bin/pip install --no-cache-dir "uv=={UV_VERSION}" && \\
    ln -sf /opt/uv/bin/uv /usr/local/bin/uv && \\
    uv --version && rustup --version && cargo --version && protoc --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class Qdrant9531To11118ImageDefault(Image):
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
        return Qdrant9531To11118ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = _test_cmd(self.pr.test_patch)

        prepare_sh = """\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK
rustc --version
cargo --version
cargo fetch --locked

GATE_CMD
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
ENV_BLOCK
cd /home/REPO
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("ENV_BLOCK\n", _ENV)
                .replace("TEST_CMD", test_cmd)
                .replace("GATE_CMD", _gate_cmd(self.pr.test_patch))
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("REPO", repo)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("qdrant", "qdrant_9531_to_11118")
class QDRANT_9531_TO_11118(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Qdrant9531To11118ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        rust_buckets = {"ok": passed_tests, "FAILED": failed_tests, "ignored": skipped_tests}
        py_buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        re_suite = re.compile(r"^MSB_SUITE\|(\S+)\s*$")
        re_rust = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
        re_py = re.compile(
            r"^(\S+\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: \(.*\))?(?:\s+\[\s*\d+%\])?$"
        )
        re_output_start = re.compile(r"^---- \S+ std(out|err) ----$")
        re_output_end = re.compile(r"^(test result:|running \d+ tests?$)")

        suite = ""
        in_output = False
        for line in log.splitlines():
            line = line.strip()
            m = re_suite.match(line)
            if m:
                suite = m.group(1)
                in_output = False
                continue
            if not suite:
                continue
            if suite.startswith("pytest:"):
                m = re_py.match(line)
                if m:
                    py_buckets[m.group(2)].add(f"{suite}::{m.group(1)}")
                continue
            if re_output_start.match(line):
                in_output = True
                continue
            if re_output_end.match(line):
                in_output = False
                continue
            if in_output:
                continue
            m = re_rust.match(line)
            if m:
                rust_buckets[m.group(2)].add(f"{suite}::{m.group(1)}")

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
