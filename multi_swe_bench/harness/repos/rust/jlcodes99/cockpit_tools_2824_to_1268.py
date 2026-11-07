from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult

_INTERVAL_NAME = "cockpit_tools_2824_to_1268"
_BASE_IMAGE = "rust:1.99.1-bookworm"
_GO_IMAGE = "golang:1.26.10-bookworm"
_NODE_IMAGE = "node:22.22-bookworm"
_TSX_VERSION = "4.20.5"
_BASE_TAG = "base-2824_to_1268"
_REPO_DIR = "/home/cockpit-tools"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_MARKER_RE = re.compile(
    r"^===== (TS FILE|TS EXIT|TS RETRY|TS RETRY EXIT|GO MODULE|GO EXIT|GO RETRY|GO RETRY EXIT"
    r"|RUST PACKAGE|RUST EXIT|RUST RETRY|RUST RETRY EXIT): (.+) =====$"
)
_TAP_SUBTEST_RE = re.compile(r"^( *)# Subtest: (.*)$")
_TAP_RESULT_RE = re.compile(r"^( *)(ok|not ok) \d+ - (.*?)(?: # (SKIP|TODO)(?:\s.*)?)?$")
_GO_BUILD_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]\s*$")
_RUST_RUNNING_RE = re.compile(r"^Running (?:unittests )?\S+ \((.+)\)$")
_RUST_TEST_RE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)(?:,.*)?$")
_RUST_HASH_RE = re.compile(r"-[0-9a-f]{16}$")

_CRATES_SH = r"""rust_packages() {
    cargo metadata --no-deps --format-version 1 < /dev/null \
        | python3 -c 'import json, sys; [print(name) for name in sorted(p["name"] for p in json.load(sys.stdin)["packages"])]'
}

go_modules() {
    find sidecars -type f -name go.mod -not -path '*/examples/*' -not -path '*/node_modules/*' 2>/dev/null | LC_ALL=C sort
}

ts_test_files() {
    find src tests -type f -name '*.test.ts' -not -path '*/node_modules/*' 2>/dev/null | LC_ALL=C sort
}
"""

_GO_FAILED_PY = r"""import json
import sys

failed = {}
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        test = event.get("Test") or ""
        if event.get("Action") == "fail" and test and "/" not in test and event.get("Package"):
            failed.setdefault(event["Package"], set()).add(test)
for package in sorted(failed):
    print(package + "\t^(" + "|".join(sorted(failed[package])) + ")$")
"""

_TEST_BODY_SH = r"""suites=0
attempt_dir=$(mktemp -d)

while IFS= read -r ts_file; do
    [ -n "$ts_file" ] || continue
    suites=$((suites + 1))
    echo "===== TS FILE: $ts_file ====="
    rc=0
    timeout 1200 tsx --test --test-reporter=tap "$ts_file" < /dev/null 2>&1 || rc=$?
    echo "===== TS EXIT: $ts_file $rc ====="
    if [ "$rc" -ne 0 ]; then
        echo "===== TS RETRY: $ts_file ====="
        rc=0
        timeout 1200 tsx --test --test-reporter=tap "$ts_file" < /dev/null 2>&1 || rc=$?
        echo "===== TS RETRY EXIT: $ts_file $rc ====="
    fi
done < <(ts_test_files)

while IFS= read -r go_mod; do
    [ -n "$go_mod" ] || continue
    suites=$((suites + 1))
    mod_dir=$(dirname "$go_mod")
    mod_path=$(awk '$1 == "module" { print $2; exit }' "$go_mod")
    echo "===== GO MODULE: $mod_dir $mod_path ====="
    rc=0
    (cd "$mod_dir" && go test -json -count=1 -timeout 30m ./... < /dev/null 2>&1) | tee "$attempt_dir/go.json" || rc=$?
    echo "===== GO EXIT: $mod_dir $rc ====="
    if [ "$rc" -ne 0 ]; then
        while IFS=$'\t' read -r go_pkg go_pattern; do
            [ -n "$go_pkg" ] || continue
            echo "===== GO RETRY: $mod_dir $mod_path ====="
            rc=0
            (cd "$mod_dir" && go test -json -count=1 -timeout 30m -run "$go_pattern" "$go_pkg" < /dev/null 2>&1) || rc=$?
            echo "===== GO RETRY EXIT: $mod_dir $rc ====="
        done < <(python3 -c "$GO_FAILED_PY" "$attempt_dir/go.json")
    fi
done < <(go_modules)

while IFS= read -r crate; do
    [ -n "$crate" ] || continue
    suites=$((suites + 1))
    echo "===== RUST PACKAGE: $crate ====="
    rc=0
    timeout 5400 cargo test -p "$crate" --tests --no-fail-fast -- --test-threads=1 < /dev/null 2>&1 | tee "$attempt_dir/rust.log" || rc=$?
    echo "===== RUST EXIT: $crate $rc ====="
    mapfile -t rust_failed < <(sed -n 's/^test \(.*\) \.\.\. FAILED$/\1/p' "$attempt_dir/rust.log" | LC_ALL=C sort -u)
    if [ "${#rust_failed[@]}" -gt 0 ]; then
        echo "===== RUST RETRY: $crate ====="
        rc=0
        timeout 5400 cargo test -p "$crate" --tests --no-fail-fast -- --test-threads=1 --exact "${rust_failed[@]}" < /dev/null 2>&1 || rc=$?
        echo "===== RUST RETRY EXIT: $crate $rc ====="
    fi
done < <(rust_packages)

if [ "$suites" -eq 0 ]; then
    echo "no test suites were discovered" >&2
    exit 1
fi
"""

_CARGO_WARM_SH = r"""cargo_warm() {
    local crate="$1"
    shift
    local messages
    local rc=0
    messages=$(mktemp)
    cargo test -p "$crate" --tests --no-run --no-fail-fast --message-format=json "$@" < /dev/null > "$messages" || rc=$?
    local verdict=0
    python3 - "$messages" "$rc" "$crate" <<'PY' || verdict=$?
import json
import sys

path, rc, crate = sys.argv[1], int(sys.argv[2]), sys.argv[3]
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
    print(f"CARGO_WARM_OK {crate} artifacts={artifacts}")
    sys.exit(0)
if foreign_errors or not local_errors:
    print(f"CARGO_WARM_FAIL {crate} rc={rc} dependency_errors={sorted(foreign_errors)}", file=sys.stderr)
    sys.exit(1)
print(f"CARGO_WARM_BASE_SOURCE_BROKEN {crate} rc={rc} artifacts={artifacts} workspace_errors={sorted(local_errors)}")
PY
    rm -f "$messages"
    return "$verdict"
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


def _script_header() -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        "\n"
        f"cd {_REPO_DIR}\n"
        "\n"
        f"{_CRATES_SH}\n"
        "GO_FAILED_PY=$(cat <<'PY'\n"
        f"{_GO_FAILED_PY}"
        "PY\n"
        ")\n"
        "\n"
    )


class CockpitTools2824To1268ImageBase(Image):
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
    git ca-certificates build-essential curl wget file pkg-config python3 patchelf xdg-utils \\
    libssl-dev libgtk-3-dev libwebkit2gtk-4.1-dev libjavascriptcoregtk-4.1-dev libsoup-3.0-dev \\
    libayatana-appindicator3-dev librsvg2-dev libxdo-dev libnm-dev \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_GO_IMAGE} /usr/local/go /usr/local/go
COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from={_NODE_IMAGE} /usr/local/include/node /usr/local/include/node

ENV CI=true \\
    GOPATH=/root/go \\
    GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false \\
    PATH=/usr/local/go/bin:/root/go/bin:${{PATH}} \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    RUST_BACKTRACE=1 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    npm_config_update_notifier=false \\
    npm_config_fund=false \\
    npm_config_audit=false

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version && go version && cargo --version && \\
    npm install -g tsx@{_TSX_VERSION} && tsx --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class CockpitTools2824To1268ImageDefault(Image):
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
        return CockpitTools2824To1268ImageBase(self.pr, self._config)

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
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            f"{_CRATES_SH}\n"
            f"{_CARGO_WARM_SH}\n"
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
            'export GOPROXY="https://proxy.golang.org|direct"\n'
            "host_target=$(rustc -vV | awk '$1 == \"host:\" { print $2 }')\n"
            "\n"
            "step npm ci\n"
            "retry npm ci --no-audit --no-fund\n"
            "\n"
            "mkdir -p dist\n"
            "[ -e dist/index.html ] || printf '<!doctype html><html><body></body></html>\\n' > dist/index.html\n"
            "\n"
            'step "cargo fetch $host_target"\n'
            'retry cargo fetch --locked --target "$host_target" < /dev/null\n'
            "\n"
            "while IFS= read -r go_mod; do\n"
            '    step "go mod download $go_mod"\n'
            '    (cd "$(dirname "$go_mod")" && retry go mod download < /dev/null)\n'
            "done < <(go_modules)\n"
            "\n"
            "while IFS= read -r go_mod; do\n"
            '    step "go test warm build $go_mod"\n'
            '    (cd "$(dirname "$go_mod")" && go test -vet=off -count=1 -run \'^$\' ./... < /dev/null)\n'
            "done < <(go_modules)\n"
            "\n"
            "while IFS= read -r crate; do\n"
            '    step "cargo test warm build $crate"\n'
            '    cargo_warm "$crate"\n'
            "done < <(rust_packages)\n"
            "\n"
            "step gate\n"
            "node -e \"require('./package.json'); require.resolve('typescript'); console.log('NODE_DEPS_OK')\"\n"
            "tsx --version\n"
            '[ -n "$(rust_packages)" ]\n'
            '[ -n "$(go_modules)" ]\n'
            "while IFS= read -r go_mod; do\n"
            '    (cd "$(dirname "$go_mod")" && GOPROXY=off go list -deps -test ./... > /dev/null < /dev/null)\n'
            "done < <(go_modules)\n"
            "while IFS= read -r crate; do\n"
            '    cargo_warm "$crate" --offline\n'
            "done < <(rust_packages)\n"
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


def _go_package_dir(mod_dir: str, mod_path: str, package: str) -> str:
    if mod_path and package == mod_path:
        return mod_dir
    if mod_path and package.startswith(mod_path + "/"):
        return f"{mod_dir}/{package[len(mod_path) + 1:]}"
    return f"{mod_dir}@{package}"


@Instance.register("jlcodes99", _INTERVAL_NAME)
@Instance.register("jlcodes99", "cockpit-tools")
class COCKPIT_TOOLS_2824_TO_1268(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return CockpitTools2824To1268ImageDefault(self.pr, self._config)

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
        retry_passed: set[str] = set()
        retry_failed: set[str] = set()
        retry_skipped: set[str] = set()
        retrying = False

        def record(name: str, status: str) -> None:
            if not name:
                return
            if retrying:
                target = {"pass": retry_passed, "fail": retry_failed}.get(status, retry_skipped)
            else:
                target = {"pass": passed_tests, "fail": failed_tests}.get(status, skipped_tests)
            target.add(name)

        section = None
        ts_file = ""
        ts_leaves = 0
        ts_stack: list[tuple[str, bool]] = []
        ts_children: list[bool] = []
        go_dir = ""
        go_path = ""
        go_ran: set[str] = set()
        go_status: dict[str, str] = {}
        rust_crate = ""
        rust_binary = ""
        rust_ran = False

        def ts_wrapper(title: str) -> bool:
            return bool(ts_file) and (title == ts_file or title.endswith("/" + ts_file))

        def flush_go() -> None:
            for pkg_dir in sorted(set(go_status) | go_ran):
                if pkg_dir in go_ran:
                    record(pkg_dir, "pass")
                elif go_status.get(pkg_dir) == "fail":
                    record(pkg_dir, "fail")
                elif go_status.get(pkg_dir) == "pass":
                    record(pkg_dir, "pass")
            go_ran.clear()
            go_status.clear()

        clean_log = _ANSI_RE.sub("", test_log)

        for raw_line in clean_log.splitlines():
            line = raw_line.rstrip("\r")
            stripped = line.strip()

            marker = _MARKER_RE.match(stripped)
            if marker:
                kind, value = marker.group(1), marker.group(2).strip()
                if kind in ("TS FILE", "TS RETRY"):
                    retrying = kind == "TS RETRY"
                    section = "ts"
                    ts_file = value
                    ts_leaves = 0
                    ts_stack = []
                    ts_children = []
                elif kind in ("TS EXIT", "TS RETRY EXIT"):
                    parts = value.rsplit(" ", 1)
                    rc = parts[1] if len(parts) == 2 else "1"
                    if ts_file:
                        if ts_leaves > 0:
                            record(ts_file, "pass")
                        elif rc != "0":
                            record(ts_file, "fail")
                    section = None
                    ts_file = ""
                    retrying = False
                elif kind in ("GO MODULE", "GO RETRY"):
                    flush_go()
                    retrying = kind == "GO RETRY"
                    parts = value.split(" ", 1)
                    go_dir = parts[0]
                    go_path = parts[1].strip() if len(parts) == 2 else ""
                    section = "go"
                elif kind in ("GO EXIT", "GO RETRY EXIT"):
                    flush_go()
                    section = None
                    retrying = False
                elif kind in ("RUST PACKAGE", "RUST RETRY"):
                    retrying = kind == "RUST RETRY"
                    section = "rust"
                    rust_crate = value
                    rust_binary = ""
                    rust_ran = False
                elif kind in ("RUST EXIT", "RUST RETRY EXIT"):
                    parts = value.rsplit(" ", 1)
                    rc = parts[1] if len(parts) == 2 else "1"
                    if rust_crate:
                        if rust_ran:
                            record(rust_crate, "pass")
                        elif rc != "0":
                            record(rust_crate, "fail")
                    section = None
                    rust_crate = ""
                    retrying = False
                continue

            if section == "ts":
                subtest = _TAP_SUBTEST_RE.match(line)
                if subtest:
                    level = len(subtest.group(1)) // 4
                    title = subtest.group(2).strip()
                    del ts_stack[level:]
                    del ts_children[level:]
                    if 0 < level <= len(ts_children):
                        ts_children[level - 1] = True
                    ts_stack.append((title, ts_wrapper(title)))
                    ts_children.append(False)
                    continue
                result = _TAP_RESULT_RE.match(line)
                if result:
                    level = len(result.group(1)) // 4
                    title = result.group(3).strip()
                    has_children = level < len(ts_children) and ts_children[level]
                    if title and not has_children and not ts_wrapper(title):
                        parents = [name for name, wrapper in ts_stack[:level] if not wrapper]
                        name = " > ".join([ts_file, *parents, title])
                        if result.group(4):
                            record(name, "skip")
                        elif result.group(2) == "ok":
                            record(name, "pass")
                        else:
                            record(name, "fail")
                        ts_leaves += 1
                    del ts_stack[level:]
                    del ts_children[level:]
                continue

            if section == "go":
                if stripped.startswith("{"):
                    try:
                        event = json.loads(stripped)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    package = event.get("Package")
                    action = event.get("Action")
                    if not package or not action:
                        continue
                    pkg_dir = _go_package_dir(go_dir, go_path, package)
                    test = event.get("Test")
                    if test:
                        if action in ("pass", "fail", "skip"):
                            record(f"{pkg_dir}::{test}", action)
                            go_ran.add(pkg_dir)
                        continue
                    if action in ("pass", "fail", "skip"):
                        if go_status.get(pkg_dir) != "fail":
                            go_status[pkg_dir] = action
                    elif action == "output":
                        build_fail = _GO_BUILD_FAIL_RE.match((event.get("Output") or "").strip())
                        if build_fail:
                            go_status[_go_package_dir(go_dir, go_path, build_fail.group(1))] = "fail"
                    continue
                build_fail = _GO_BUILD_FAIL_RE.match(stripped)
                if build_fail:
                    go_status[_go_package_dir(go_dir, go_path, build_fail.group(1))] = "fail"
                continue

            if section == "rust":
                running = _RUST_RUNNING_RE.match(stripped)
                if running:
                    binary = running.group(1).strip().replace("\\", "/").rsplit("/", 1)[-1]
                    rust_binary = _RUST_HASH_RE.sub("", binary)
                    rust_ran = True
                    continue
                test = _RUST_TEST_RE.match(stripped)
                if test:
                    owner = rust_binary or rust_crate.replace("-", "_")
                    status = {"ok": "pass", "FAILED": "fail"}.get(test.group(2), "skip")
                    record(f"{owner}::{test.group(1)}", status)
                continue

        flush_go()

        for name in sorted(failed_tests & (retry_passed | retry_failed | retry_skipped)):
            if name in retry_failed:
                continue
            failed_tests.discard(name)
            if name in retry_skipped:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
