from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL_NAME = "rustfs_8901_to_8459"
_BASE_IMAGE = "rust:1.99.1-bookworm"
_RUST_TOOLCHAIN = "1.99.1"
_NEXTEST_VERSION = "0.9.138"
_HELM_VERSION = "3.19.0"
_YQ_VERSION = "4.54.1"
_PROTOC_VERSION = "35.1"
_E2E_FEATURES = "e2e-test-hooks"
_BASE_TAG = "base-8901_to_8459"
_REPO_DIR = "/home/rustfs"
_AMD64_RUSTFLAGS = "-C linker-features=-lld -C link-arg=-fuse-ld=gold"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_MARKER_RE = re.compile(
    r"^===== (RUST BUILD|RUST BUILD EXIT|RUST CRATE|E2E SERVER|E2E SERVER EXIT|E2E CRATE"
    r"|NEXTEST REPORT|NEXTEST REPORT EXIT|SCRIPT TEST|SCRIPT TEST EXIT): (.+) =====$"
)
_NEXTEST_CASE_RE = re.compile(r"^NEXTEST CASE\t(pass|fail|skip)\t(.+)$")
_UNITTEST_LINE_RE = re.compile(
    r"^(\w+) \(([\w.]+)\)(?: \(.*\))? \.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
)
_UNITTEST_HEAD_RE = re.compile(r"^(\w+) \(([\w.]+)\)$")
_UNITTEST_TAIL_RE = re.compile(r" \.\.\. (ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b")
_UNITTEST_STATUS = {
    "ok": "pass",
    "expected failure": "pass",
    "FAIL": "fail",
    "ERROR": "fail",
    "unexpected success": "fail",
    "skipped": "skip",
}

_BUILD_STATUS_PY = r"""import json
import os
import sys

meta_path, messages_path, rc, scope = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
with open(meta_path, encoding="utf-8") as handle:
    meta = json.load(handle)
root = meta["workspace_root"]
members = set(meta["workspace_members"])
built = set()
with open(messages_path, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("reason") == "compiler-artifact" and (message.get("profile") or {}).get("test"):
            built.add(message.get("package_id"))
testable_kinds = {"lib", "rlib", "proc-macro", "bin", "test"}
for package in sorted(meta["packages"], key=lambda item: item["name"]):
    if package["id"] not in members:
        continue
    if (package["name"] == "e2e_test") != (scope == "e2e"):
        continue
    if not any(target.get("test", True) and testable_kinds & set(target.get("kind", [])) for target in package["targets"]):
        continue
    rel_dir = os.path.relpath(os.path.dirname(package["manifest_path"]), root)
    status = "built" if rc == 0 or package["id"] in built else "broken"
    print(f"{status}\t{package['name']}\t{rel_dir}")
"""

_JUNIT_PY = r"""import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError) as error:
    print(f"junit unreadable: {error}", file=sys.stderr)
    sys.exit(0)
suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
for suite in suites:
    suite_name = suite.get("name") or ""
    for case in suite.findall("testcase"):
        name = (case.get("name") or "").strip()
        owner = (case.get("classname") or suite_name).strip()
        if not name:
            continue
        tags = {child.tag for child in case}
        if "failure" in tags or "error" in tags:
            status = "fail"
        elif "skipped" in tags:
            status = "skip"
        else:
            status = "pass"
        print(f"NEXTEST CASE\t{status}\t{owner}::{name}" if owner else f"NEXTEST CASE\t{status}\t{name}")
"""

_CARGO_WARM_PY = r"""import json
import sys

path, rc, label = sys.argv[1], int(sys.argv[2]), sys.argv[3]
artifacts = 0
local_errors = set()
foreign_errors = set()
with open(path, encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reason = message.get("reason")
        if reason == "compiler-artifact":
            artifacts += 1
        elif reason == "compiler-message":
            diagnostic = message.get("message") or {}
            if str(diagnostic.get("level", "")).startswith("error"):
                package_id = message.get("package_id", "")
                if package_id.startswith("path+"):
                    local_errors.add(package_id)
                else:
                    foreign_errors.add(package_id)
                sys.stderr.write(diagnostic.get("rendered") or "")
if rc == 0:
    print(f"CARGO_WARM_OK {label} artifacts={artifacts}")
    sys.exit(0)
if foreign_errors or not local_errors:
    print(f"CARGO_WARM_FAIL {label} rc={rc} dependency_errors={sorted(foreign_errors)}", file=sys.stderr)
    sys.exit(1)
print(f"CARGO_WARM_BASE_SOURCE_BROKEN {label} rc={rc} artifacts={artifacts} workspace_errors={sorted(local_errors)}")
"""

_HELPERS_SH = r"""e2e_modules() {
    [ -f /home/test.patch ] || return 0
    sed -n 's#^diff --git a/crates/e2e_test/src/\(.*\)\.rs b/.*$#\1#p' /home/test.patch \
        | sed -e 's#/mod$##' -e 's#/#::#g' \
        | grep -vxE 'lib|main|bin(::.*)?' \
        | LC_ALL=C sort -u
}

script_tests() {
    find scripts -type f \( -name 'test_*.py' -o -name 'test_*.sh' \) 2>/dev/null | LC_ALL=C sort
}
"""

_TEST_BODY_SH = r"""suites=0
work=$(mktemp -d)
tool_cfg="$work/nextest-tool.toml"
printf '[profile.ci]\nslow-timeout = { period = "120s", terminate-after = 10, grace-period = "10s" }\n\n[profile.e2e-full]\nslow-timeout = { period = "120s", terminate-after = 10, grace-period = "10s" }\n' > "$tool_cfg"

nextest_report() {
    local scope="$1"
    local junit="$2"
    local rc="$3"
    echo "===== NEXTEST REPORT: $scope ====="
    if [ -f "$junit" ]; then
        python3 -c "$JUNIT_PY" "$junit"
    fi
    echo "===== NEXTEST REPORT EXIT: $scope $rc ====="
}

cargo metadata --no-deps --format-version 1 < /dev/null > "$work/metadata.json"

echo "===== RUST BUILD: workspace ====="
rc=0
timeout 7200 cargo test --workspace --exclude e2e_test --no-run --message-format json-render-diagnostics < /dev/null > "$work/build.json" || rc=$?
excludes=()
while IFS=$'\t' read -r status name rel_dir; do
    [ -n "$name" ] || continue
    suites=$((suites + 1))
    echo "===== RUST CRATE: $status $rel_dir ====="
    if [ "$status" = broken ]; then
        excludes+=(--exclude "$name")
    fi
done < <(python3 -c "$BUILD_STATUS_PY" "$work/metadata.json" "$work/build.json" "$rc" workspace)
echo "===== RUST BUILD EXIT: workspace $rc ====="

rm -f target/nextest/ci/junit.xml
rc=0
NEXTEST_HIDE_PROGRESS_BAR=1 timeout 10800 cargo nextest run --profile ci --tool-config-file "mswebench:$tool_cfg" \
    --workspace --exclude e2e_test "${excludes[@]}" \
    --no-fail-fast --retries 2 --status-level fail --final-status-level fail --no-tests=pass < /dev/null 2>&1 || rc=$?
nextest_report workspace target/nextest/ci/junit.xml "$rc"

mapfile -t e2e_targets < <(e2e_modules)
if [ "${#e2e_targets[@]}" -gt 0 ]; then
    e2e_filter="test(/^($(IFS='|'; echo "${e2e_targets[*]}"))::/)"
    echo "===== E2E SERVER: $e2e_filter ====="
    server_rc=0
    timeout 7200 python3 scripts/e2e_binary.py build --features __E2E_FEATURES__ < /dev/null 2>&1 || server_rc=$?
    echo "===== E2E SERVER EXIT: $server_rc ====="
    rc=0
    timeout 7200 cargo test -p e2e_test --no-run --message-format json-render-diagnostics < /dev/null > "$work/e2e-build.json" || rc=$?
    e2e_ready=0
    while IFS=$'\t' read -r status name rel_dir; do
        [ -n "$name" ] || continue
        suites=$((suites + 1))
        if [ "$server_rc" -ne 0 ]; then
            status=broken
        fi
        echo "===== E2E CRATE: $status $rel_dir ====="
        if [ "$status" = built ]; then
            e2e_ready=1
        fi
    done < <(python3 -c "$BUILD_STATUS_PY" "$work/metadata.json" "$work/e2e-build.json" "$rc" e2e)
    if [ "$e2e_ready" -eq 1 ]; then
        rm -f target/nextest/e2e-full/junit.xml
        rc=0
        NEXTEST_HIDE_PROGRESS_BAR=1 timeout 10800 python3 scripts/e2e_binary.py run --features __E2E_FEATURES__ -- \
            cargo nextest run --profile e2e-full --tool-config-file "mswebench:$tool_cfg" -p e2e_test -E "$e2e_filter" \
            --no-fail-fast --retries 2 --status-level fail --final-status-level fail --no-tests=pass < /dev/null 2>&1 || rc=$?
        nextest_report e2e target/nextest/e2e-full/junit.xml "$rc"
    fi
fi

while IFS= read -r script; do
    [ -n "$script" ] || continue
    suites=$((suites + 1))
    echo "===== SCRIPT TEST: $script ====="
    rc=0
    case "$script" in
        *.py)
            if grep -q unittest "$script"; then
                PYTHONPATH="$(dirname "$script")" timeout 900 python3 -m unittest -v -b "$(basename "$script" .py)" < /dev/null 2>&1 || rc=$?
            else
                timeout 900 python3 "$script" < /dev/null 2>&1 || rc=$?
            fi
            ;;
        *)
            timeout 900 bash "$script" < /dev/null 2>&1 || rc=$?
            ;;
    esac
    echo "===== SCRIPT TEST EXIT: $script $rc ====="
done < <(script_tests)

if [ "$suites" -eq 0 ]; then
    echo "no test suites were discovered" >&2
    exit 1
fi
""".replace("__E2E_FEATURES__", _E2E_FEATURES)

_CARGO_WARM_SH = r"""cargo_warm() {
    local label="$1"
    shift
    local messages
    local rc=0
    local verdict
    messages=$(mktemp)
    if [ "$WARM_MODE" = check ]; then
        cargo check --all-targets --message-format json "$@"
    else
        cargo test --no-run --message-format json "$@"
    fi < /dev/null > "$messages" || rc=$?
    verdict=$(python3 -c "$CARGO_WARM_PY" "$messages" "$rc" "$label")
    rm -f "$messages"
    echo "$verdict"
    CARGO_WARM_STATE="${verdict%% *}"
}
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: repo has uncommitted changes"
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes: repo is clean"
exit 0
"""


def _embed_py(variable: str, program: str) -> str:
    return f"{variable}=$(cat <<'PY'\n{program}PY\n)\n"


def _script_header() -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        f'export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="{_AMD64_RUSTFLAGS}"\n'
        "\n"
        f"cd {_REPO_DIR}\n"
        "\n"
        f"{_HELPERS_SH}\n"
        f"{_embed_py('BUILD_STATUS_PY', _BUILD_STATUS_PY)}"
        f"{_embed_py('JUNIT_PY', _JUNIT_PY)}"
        "\n"
    )


class Rustfs8901To8459ImageBase(Image):
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
        return _BASE_IMAGE

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

FROM {_BASE_IMAGE}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential cmake clang perl pkg-config libssl-dev \\
    curl wget file xz-utils unzip procps python3 ripgrep jq \\
    && rm -rf /var/lib/apt/lists/*

ENV CI=true \\
    RUSTUP_TOOLCHAIN={_RUST_TOOLCHAIN} \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    CARGO_NET_GIT_FETCH_WITH_CLI=true \\
    CARGO_BUILD_JOBS=4 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    CARGO_PROFILE_DEV_STRIP=debuginfo \\
    CARGO_PROFILE_TEST_STRIP=debuginfo \\
    RUST_BACKTRACE=1 \\
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C linker-features=-lld" \\
    PROTOC=/usr/local/bin/protoc \\
    NEXTEST_HIDE_PROGRESS_BAR=1

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) rust_arch=x86_64; protoc_arch=x86_64 ;; \\
        arm64) rust_arch=aarch64; protoc_arch=aarch_64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    mkdir -p /tmp/nextest; \\
    curl -fsSL --retry 5 "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-{_NEXTEST_VERSION}/cargo-nextest-{_NEXTEST_VERSION}-${{rust_arch}}-unknown-linux-gnu.tar.gz" \\
        | tar -xzf - -C /tmp/nextest; \\
    install -m 0755 /tmp/nextest/cargo-nextest /usr/local/cargo/bin/cargo-nextest; \\
    rm -rf /tmp/nextest; \\
    curl -fsSL --retry 5 "https://get.helm.sh/helm-v{_HELM_VERSION}-linux-${{arch}}.tar.gz" | tar -xzf - -C /tmp; \\
    install -m 0755 "/tmp/linux-${{arch}}/helm" /usr/local/bin/helm; \\
    rm -rf "/tmp/linux-${{arch}}"; \\
    curl -fsSL --retry 5 -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v{_YQ_VERSION}/yq_linux_${{arch}}"; \\
    chmod 0755 /usr/local/bin/yq; \\
    curl -fsSL --retry 5 -o /tmp/protoc.zip "https://github.com/protocolbuffers/protobuf/releases/download/v{_PROTOC_VERSION}/protoc-{_PROTOC_VERSION}-linux-${{protoc_arch}}.zip"; \\
    unzip -q -o /tmp/protoc.zip -d /usr/local bin/protoc 'include/*'; \\
    chmod 0755 /usr/local/bin/protoc; \\
    rm -f /tmp/protoc.zip; \\
    rustc --version; cargo --version; cargo nextest --version; helm version --short; yq --version; protoc --version; python3 --version

RUN chmod go-w /root && git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Rustfs8901To8459ImageDefault(Image):
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
        return Rustfs8901To8459ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare_sh = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "\n"
            f"cd {_REPO_DIR}\n"
            "\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            f'if ! git cat-file -e "{self.pr.base.sha}^{{commit}}"; then\n'
            f"    git fetch --no-tags origin {self.pr.base.sha}\n"
            "fi\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "git ls-files -z | xargs -0 touch -h -d \"@$(git log -1 --format=%ct HEAD)\"\n"
            "\n"
            "export CARGO_BUILD_JOBS=6\n"
            f'export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="{_AMD64_RUSTFLAGS}"\n'
            "\n"
            f"{_embed_py('CARGO_WARM_PY', _CARGO_WARM_PY)}"
            "\n"
            f"{_CARGO_WARM_SH}\n"
            f"{_HELPERS_SH}\n"
            "e2e_needed=0\n"
            'if [ -n "$(e2e_modules)" ]; then\n'
            "    e2e_needed=1\n"
            "fi\n"
            "\n"
            "step() {\n"
            '    echo "===== PREPARE STEP: $* [$(date -u +%H:%M:%S)] ====="\n'
            "}\n"
            "\n"
            "retry() {\n"
            "    local attempt=1\n"
            '    until "$@"; do\n'
            '        if [ "$attempt" -ge 5 ]; then\n'
            '            echo "retry: giving up after $attempt attempts: $*" >&2\n'
            "            return 1\n"
            "        fi\n"
            "        attempt=$((attempt + 1))\n"
            '        echo "retry: attempt $attempt in $((attempt * 15))s: $*" >&2\n'
            "        sleep $((attempt * 15))\n"
            "    done\n"
            "}\n"
            "\n"
            "host_target=$(rustc -vV | awk '$1 == \"host:\" { print $2 }')\n"
            "WARM_MODE=test\n"
            'if [ "$(uname -m)" != aarch64 ]; then\n'
            "    WARM_MODE=check\n"
            "fi\n"
            "\n"
            'step "cargo fetch $host_target"\n'
            'retry cargo fetch --locked --target "$host_target" < /dev/null\n'
            "\n"
            'step "cargo $WARM_MODE warm build workspace"\n'
            "cargo_warm workspace --locked --workspace --exclude e2e_test\n"
            "workspace_state=$CARGO_WARM_STATE\n"
            "\n"
            'if [ "$workspace_state" = CARGO_WARM_OK ] && [ "$e2e_needed" -eq 1 ]; then\n'
            '    if [ "$WARM_MODE" = test ]; then\n'
            f'        step "e2e server build {_E2E_FEATURES}"\n'
            f"        python3 scripts/e2e_binary.py build --features {_E2E_FEATURES} < /dev/null\n"
            "    fi\n"
            '    step "cargo $WARM_MODE warm build e2e_test"\n'
            "    cargo_warm e2e_test --locked -p e2e_test\n"
            "fi\n"
            "\n"
            "step gate\n"
            "python3 -c \"import tomllib, unittest, xml.etree.ElementTree; print('PYTHON_OK')\"\n"
            "cargo nextest --version\n"
            "helm version --short\n"
            "yq --version\n"
            "protoc --version\n"
            "rg --version\n"
            "cargo_warm workspace --offline --workspace --exclude e2e_test\n"
            'if [ "$workspace_state" = CARGO_WARM_OK ] && [ "$WARM_MODE" = test ]; then\n'
            "    cargo nextest list --profile ci --offline --workspace --exclude e2e_test < /dev/null > /dev/null\n"
            "    test -x target/debug/rustfs\n"
            "fi\n"
            'if [ "$workspace_state" = CARGO_WARM_OK ] && [ "$e2e_needed" -eq 1 ]; then\n'
            "    cargo_warm e2e_test --offline -p e2e_test\n"
            "fi\n"
            "echo DEPS_OK\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", f"{_script_header()}{_TEST_BODY_SH}"),
            File(
                ".",
                "test-run.sh",
                f"{_script_header()}"
                "git apply --whitespace=nowarn /home/test.patch\n"
                "\n"
                f"{_TEST_BODY_SH}",
            ),
            File(
                ".",
                "fix-run.sh",
                f"{_script_header()}"
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                "\n"
                f"{_TEST_BODY_SH}",
            ),
        ]

    def dockerfile(self) -> str:
        name = self.dependency().image_full_name()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {name}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("rustfs", _INTERVAL_NAME)
@Instance.register("rustfs", "rustfs")
class RUSTFS_8901_TO_8459(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Rustfs8901To8459ImageDefault(self.pr, self._config)

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

        def record(name: str, status: str) -> None:
            if not name:
                return
            {"pass": passed_tests, "fail": failed_tests}.get(status, skipped_tests).add(name)

        def unittest_name(script: str, method: str, dotted: str) -> str:
            parts = dotted.split(".")
            if parts and parts[-1] == method:
                parts = parts[:-1]
            owner = parts[-1] if len(parts) > 1 else ""
            return f"{script}::{owner}::{method}" if owner else f"{script}::{method}"

        section = None
        script = ""
        pending: tuple[str, str] | None = None

        clean_log = _ANSI_RE.sub("", test_log)

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip("\r")
            stripped = line.strip()

            marker = _MARKER_RE.match(stripped)
            if marker:
                kind, value = marker.group(1), marker.group(2).strip()
                if kind == "NEXTEST REPORT":
                    section = "nextest"
                elif kind == "SCRIPT TEST":
                    section = "script"
                    script = value
                    pending = None
                elif kind == "SCRIPT TEST EXIT":
                    parts = value.rsplit(" ", 1)
                    rc = parts[1] if len(parts) == 2 else "1"
                    if script:
                        record(script, "pass" if rc == "0" else "fail")
                    section = None
                    script = ""
                    pending = None
                else:
                    section = None
                continue

            if section == "nextest":
                case = _NEXTEST_CASE_RE.match(line)
                if case:
                    record(case.group(2).strip(), case.group(1))
                continue

            if section == "script":
                single = _UNITTEST_LINE_RE.match(stripped)
                if single:
                    record(
                        unittest_name(script, single.group(1), single.group(2)),
                        _UNITTEST_STATUS[single.group(3)],
                    )
                    pending = None
                    continue
                head = _UNITTEST_HEAD_RE.match(stripped)
                if head:
                    pending = (head.group(1), head.group(2))
                    continue
                if pending:
                    tail = _UNITTEST_TAIL_RE.search(stripped)
                    if tail:
                        record(unittest_name(script, pending[0], pending[1]), _UNITTEST_STATUS[tail.group(1)])
                        pending = None
                continue

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
