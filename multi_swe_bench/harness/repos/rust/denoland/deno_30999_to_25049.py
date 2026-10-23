from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "denoland"
_REPO = "deno"
_ERA_KEY = "deno_30999_to_25049"
_BASE_TAG = "base-30999_to_25049"
_RUST_IMAGE = "rust:1.90.0-slim-bookworm"
_APT_PACKAGES = (
    "git ca-certificates curl build-essential cmake pkg-config libssl-dev "
    "protobuf-compiler clang libclang-dev nodejs npm"
)
_SUBMODULE = "tests/util/std"
_SUBMODULE_GATE = "tests/util/std/assert/mod.ts"
_TEST_PACKAGE = "cli_tests"
_INTEGRATION_TARGET = "integration_tests"
_SPECS_TARGET = "specs"
_JS_UNIT_MODULE = "js_unit_tests"
_NODE_UNIT_MODULE = "node_unit_tests"
_BIN_PACKAGE = "deno"
_SERVER_PACKAGE = "test_server"
_BIN_PATH = "./target/debug/deno"
_SERVER_PATH = "./target/debug/test_server"
_FALLBACK_SPEC = "crate:cli"
_TEST_THREADS = "4"

_AWK_FEATURE = (
    "awk '/^\\[features\\]/{f=1;next} /^\\[/{f=0} "
    "f&&/^[[:space:]]*run[[:space:]]*=/{ok=1} END{exit !ok}' tests/Cargo.toml"
)

_AWK_MODULE = (
    "awk -v want=\"$value.rs\" 'index($0, \"#[path = \\\"\" want \"\\\"]\")==1 "
    "{getline; sub(/;.*/, \"\", $2); print $2; exit}' tests/integration/mod.rs"
)

_AWK_PACKAGE = (
    "awk -F'\"' '/^\\[package\\]/{p=1;next} p&&/^name[[:space:]]*=/{print $2; exit}' "
    "\"$value/Cargo.toml\""
)

_MITM_PROXY_ARGS = (
    'ARG http_proxy=""\n'
    'ARG https_proxy=""\n'
    'ARG HTTP_PROXY=""\n'
    'ARG HTTPS_PROXY=""\n'
    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
)

_BASE_ENV_BLOCK = (
    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
    "    LANG=C.UTF-8 \\\n"
    "    LC_ALL=C.UTF-8 \\\n"
    "    TZ=UTC \\\n"
    "    http_proxy=${http_proxy} \\\n"
    "    https_proxy=${https_proxy} \\\n"
    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
    "    no_proxy=${no_proxy} \\\n"
    "    NO_PROXY=${NO_PROXY} \\\n"
    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    CURL_CA_BUNDLE=${CA_CERT_PATH} \\\n"
    "    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
    "    CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
    "    RUSTUP_HOME=/usr/local/rustup \\\n"
    "    CARGO_HOME=/usr/local/cargo \\\n"
    "    CARGO_PROFILE_DEV_DEBUG=0 \\\n"
    "    CARGO_PROFILE_TEST_DEBUG=0 \\\n"
    "    CARGO_INCREMENTAL=0 \\\n"
    "    CARGO_NET_RETRY=5 \\\n"
    "    CARGO_TERM_COLOR=never \\\n"
    "    RUST_BACKTRACE=1 \\\n"
    "    DENO_NO_UPDATE_CHECK=1 \\\n"
    "    PATH=/usr/local/cargo/bin:$PATH \\\n"
    "    CI=true"
)

_MITM_CERT_SYMLINKS = (
    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
)

_HARDENING_BLOCK = """RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)\""""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_RE = re.compile(
    r"^test\s+(?P<id>\S.*?)\s+\.\.\.\s+(?P<status>ok|FAILED|fail|ignored)"
    r"(?:\s+\(\d+(?:\.\d+)?m?s\))?$"
)


def _submodule_scrub_block(repo: str) -> str:
    return f"""RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
    fi"""


def _classify(path: str) -> str | None:
    if path.endswith(".ts"):
        stem = path.rsplit("/", 1)[-1][: -len(".ts")]
        if stem.endswith("_test"):
            if path.startswith("tests/unit/"):
                return f"js:{stem}"
            if path.startswith("tests/unit_node/") and "/testdata/" not in path:
                return f"node:{stem}"
    if path.startswith("tests/integration/") and path.endswith("_tests.rs"):
        return "int:" + path.rsplit("/", 1)[-1][: -len(".rs")]
    if path.startswith("tests/specs/"):
        parts = path.split("/")
        if len(parts) > 3:
            return f"spec:{parts[2]}"
        return None
    if path.startswith("tests/napi/"):
        return "crate:tests/napi"
    if path.startswith("cli/"):
        return "crate:cli"
    if path.startswith("ext/"):
        parts = path.split("/")
        if len(parts) > 2:
            return f"crate:ext/{parts[1]}"
        return None
    if path.startswith("runtime/"):
        return "crate:runtime"
    return None


_SPEC_ROOT = "tests/specs/"
_SPEC_MANIFEST = "__test__.jsonc"

_P2P_SPECS: dict[int, list[str]] = {
    27973: [
        "spec:specs::check::workspace_compiler_options_root_dirs",
        "spec:specs::run::sloppy_imports",
        "spec:specs::lint::sloppy_imports_dts",
        "spec:specs::lint::sloppy_imports_no_incremental_cache",
    ],
}

_PINNED_SPECS: dict[int, list[str]] = {
    26867: ["spec:specs::npm::npmrc_tarball_other_server"],
    33039: [
        "unitnode:_fs::_fs_handle_test",
        "unitnode:_fs::_fs_raw_fd_test",
    ],
}


def _spec_filter(path: str, manifest_dirs: list[str]) -> str:
    file_dir = path.rsplit("/", 1)[0]
    owners = [d for d in manifest_dirs if f"{file_dir}/".startswith(f"{d}/")]
    if owners:
        test_dir = max(owners, key=len)
        return "spec:specs::" + test_dir[len(_SPEC_ROOT) :].replace("/", "::")
    return f"specfile:{path}"


def _test_specs(test_patch: str) -> str:
    paths = re.findall(r"^\+\+\+ b/(\S+)", test_patch or "", re.MULTILINE)
    manifest_dirs = [
        p.rsplit("/", 1)[0]
        for p in paths
        if p.startswith(_SPEC_ROOT) and p.endswith(f"/{_SPEC_MANIFEST}")
    ]
    specs: list[str] = []
    for path in paths:
        if path.startswith(_SPEC_ROOT):
            spec = _spec_filter(path, manifest_dirs) if path.count("/") > 2 else None
        else:
            spec = _classify(path)
        if spec and spec not in specs:
            specs.append(spec)
    if not specs:
        specs.append(_FALLBACK_SPEC)
    return " ".join(specs)


def _feature_detect() -> str:
    return (
        'CLI_TESTS_FEATURES=""\n'
        f"if [ -f tests/Cargo.toml ] && {_AWK_FEATURE}; then\n"
        '    CLI_TESTS_FEATURES="--features run"\n'
        "fi"
    )


def _test_command(specs: str) -> str:
    cargo_integration = (
        f"cargo test -p {_TEST_PACKAGE} $CLI_TESTS_FEATURES "
        f"--test {_INTEGRATION_TARGET} --"
    )
    return f"""{_feature_detect()}

export RUST_BACKTRACE=0

cargo build -p {_BIN_PACKAGE} --bin {_BIN_PACKAGE}
cargo build -p {_SERVER_PACKAGE} --bin {_SERVER_PACKAGE}

TEST_SPECS="{specs}"
RC=0
for spec in $TEST_SPECS; do
    kind="${{spec%%:*}}"
    value="${{spec#*:}}"
    case "$kind" in
        js)
            {cargo_integration} "{_JS_UNIT_MODULE}::$value" --test-threads={_TEST_THREADS} 2>&1 || RC=$?
            ;;
        node)
            {cargo_integration} "{_NODE_UNIT_MODULE}::$value" --test-threads={_TEST_THREADS} 2>&1 || RC=$?
            ;;
        int)
            module="$({_AWK_MODULE})"
            if [ -z "$module" ]; then
                module="$value"
            fi
            {cargo_integration} "$module::" --test-threads={_TEST_THREADS} 2>&1 || RC=$?
            ;;
        spec)
            cargo test -p {_TEST_PACKAGE} $CLI_TESTS_FEATURES --test {_SPECS_TARGET} -- "$value" 2>&1 || RC=$?
            ;;
        specfile)
            dir="$(dirname "$value")"
            while [ "$dir" != "{_SPEC_ROOT.rstrip('/')}" ] && [ ! -f "$dir/{_SPEC_MANIFEST}" ]; do
                dir="$(dirname "$dir")"
            done
            if [ "$dir" = "{_SPEC_ROOT.rstrip('/')}" ]; then
                echo "no {_SPEC_MANIFEST} owns $value" >&2
                continue
            fi
            filter="specs::$(echo "${{dir#{_SPEC_ROOT}}}" | sed 's|/|::|g')"
            cargo test -p {_TEST_PACKAGE} $CLI_TESTS_FEATURES --test {_SPECS_TARGET} -- "$filter" 2>&1 || RC=$?
            ;;
        unitnode)
            cargo build -p unit_node_tests --test unit_node 2>&1 || true
            cargo test -p unit_node_tests --test unit_node -- "$value" 2>&1 || RC=$?
            ;;
        crate)
            pkg="$({_AWK_PACKAGE})"
            if [ -z "$pkg" ]; then
                echo "unresolved cargo package for $value" >&2
                exit 1
            fi
            cargo test -p "$pkg" -- --test-threads={_TEST_THREADS} 2>&1 || RC=$?
            ;;
    esac
done

exit $RC"""


class Deno30999To25049ImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {_RUST_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{_MITM_PROXY_ARGS}

{_BASE_ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{_MITM_CERT_SYMLINKS}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        {_APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class Deno30999To25049ImageDefault(Image):
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
        return Deno30999To25049ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        specs = _PINNED_SPECS.get(self.pr.number) or _test_specs(self.pr.test_patch).split()
        specs += [s for s in _P2P_SPECS.get(self.pr.number, []) if s not in specs]
        test_cmd = _test_command(" ".join(specs))

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "GIT_TREE_CLEAN"
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

git config --global --add safe.directory '*'

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

rustup show
rustup default "$(rustup show active-toolchain | cut -d' ' -f1)"

git submodule update --init --recursive -- {_SUBMODULE}

{_feature_detect()}

cargo fetch --locked
cargo build --locked -p {_BIN_PACKAGE} --bin {_BIN_PACKAGE}
cargo build --locked -p {_SERVER_PACKAGE} --bin {_SERVER_PACKAGE}
if grep -q '^name = "{_TEST_PACKAGE}"' tests/Cargo.toml 2>/dev/null; then
    cargo build --locked -p {_TEST_PACKAGE} $CLI_TESTS_FEATURES --test {_INTEGRATION_TARGET}
else
    cargo build --locked -p unit_node_tests --test unit_node
fi

rustc --version > /dev/null
cargo --version > /dev/null
test -s {_SUBMODULE_GATE}
test -x {_BIN_PATH}
test -x {_SERVER_PATH}
{_BIN_PATH} eval 'console.log("DEPS_OK")'
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd

{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch

{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        repo = self.pr.repo

        sections = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "COPY check_git_changes.sh /home/\n"
            "COPY prepare.sh /home/\n"
            "COPY run.sh /home/\n"
            "COPY test-run.sh /home/\n"
            "COPY fix-run.sh /home/"
        )

        sections.append("RUN bash /home/prepare.sh")
        sections.append(_HARDENING_BLOCK)
        sections.append(_submodule_scrub_block(repo))

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register(_ORG, _ERA_KEY)
class DENO_30999_TO_25049(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Deno30999To25049ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        final_status: dict[str, str] = {}
        for raw in _ANSI_RE.sub("", log).splitlines():
            match = _TEST_RE.match(raw.strip())
            if not match:
                continue
            final_status[match.group("id").strip()] = match.group("status")

        passed_tests: set[str] = {n for n, s in final_status.items() if s == "ok"}
        failed_tests: set[str] = {n for n, s in final_status.items() if s in ("FAILED", "fail")}
        skipped_tests: set[str] = {n for n, s in final_status.items() if s == "ignored"}

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
