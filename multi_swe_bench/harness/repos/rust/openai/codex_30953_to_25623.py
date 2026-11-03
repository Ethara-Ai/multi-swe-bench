from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "rust:1.95.0-bookworm"
_BASE_TAG = "base-30953_to_25623"
_REPO_DIR = "/home/codex"
_CARGO_DIR = "/home/codex/codex-rs"
_TEST_ENV = "/home/codex_test_env"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CRATE_RE = re.compile(r"^=== MSB_CRATE: (\S+) ===$")
_PY_FILE_RE = re.compile(r"^=== MSB_PYFILE: (\S+) ===$")
_TARGET_RE = re.compile(r"^Running (?P<unit>unittests )?(?P<target>\S+) \((?P<binary>[^)]+)\)")
_BIN_HASH_RE = re.compile(r"-[0-9a-f]{8,}$")
_DOCTESTS_RE = re.compile(r"^Doc-tests\b")
_RESULT_RE = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)")
_DOC_NAME_RE = re.compile(r"^(\S+\.rs) - (.*?)\s*\(line (\d+)\)(.*)$")
_PY_RESULT_RE = re.compile(
    r"^(\S+) \(([^)]+)\)(?: .*)? \.\.\. (ok|FAIL|ERROR|skipped.*)$"
)
_PY_HEAD_RE = re.compile(r"^(\S+) \(([^)]+)\)$")
_PY_STATUS_RE = re.compile(r"^.* \.\.\. (ok|FAIL|ERROR|skipped.*)$")

_DERIVE_TARGETS_SH = """python3 - "$REPO_ROOT" "$CARGO_DIR" /home/test.patch /tmp/msb-packages.txt /tmp/msb-pyfiles.txt <<'PYEOF'
import json
import os
import re
import subprocess
import sys

repo_root, cargo_dir, patch_path, pkg_out, py_out = sys.argv[1:6]

with open(patch_path, encoding="utf-8", errors="replace") as handle:
    patch = handle.read()

paths = sorted(set(re.findall(r"^\\+\\+\\+ b/(.+)$", patch, re.M)))

manifest_dirs = set()
py_files = []
for path in paths:
    abs_path = os.path.join(repo_root, path)
    if path.endswith(".py") and os.path.basename(path).startswith("test_"):
        py_files.append(path)
    current = os.path.dirname(abs_path)
    while current.startswith(cargo_dir):
        if os.path.isfile(os.path.join(current, "Cargo.toml")):
            manifest_dirs.add(os.path.realpath(current))
            break
        current = os.path.dirname(current)

packages = []
if manifest_dirs:
    meta = json.loads(
        subprocess.check_output(
            ["cargo", "metadata", "--offline", "--no-deps", "--format-version", "1"],
            cwd=cargo_dir,
            text=True,
        )
    )
    members = set(meta["workspace_members"])
    for pkg in sorted(meta["packages"], key=lambda p: p["name"]):
        if pkg["id"] not in members:
            continue
        pkg_dir = os.path.realpath(os.path.dirname(pkg["manifest_path"]))
        if pkg_dir in manifest_dirs:
            rel = os.path.relpath(pkg_dir, repo_root)
            packages.append((pkg["name"], rel))

with open(pkg_out, "w", encoding="utf-8") as handle:
    for name, rel in packages:
        handle.write(f"{name} {rel}\\n")

with open(py_out, "w", encoding="utf-8") as handle:
    for path in py_files:
        handle.write(f"{path}\\n")
PYEOF
"""

_TEST_BODY_SH = """fatal=0

cd "$CARGO_DIR"
while read -r pkg dir; do
    [ -n "$pkg" ] || continue
    echo "=== MSB_CRATE: $dir ==="
    rc=0
    cargo test --offline -p "$pkg" --no-fail-fast < /dev/null > /tmp/msb-cargo.log 2>&1 || rc=$?
    cat /tmp/msb-cargo.log
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 101 ]; then
        echo "cargo test exited with $rc for package $pkg" >&2
        fatal=$rc
    fi
    if grep -qE '^error: could not compile' /tmp/msb-cargo.log; then
        echo "cargo test failed to build package $pkg" >&2
        fatal=1
    fi
done < /tmp/msb-packages.txt

cd "$REPO_ROOT"
while read -r pyfile; do
    [ -n "$pyfile" ] || continue
    [ -f "$pyfile" ] || continue
    echo "=== MSB_PYFILE: $pyfile ==="
    rc=0
    python3 -m unittest discover -v -s "$(dirname "$pyfile")" -t "$(dirname "$pyfile")" -p "$(basename "$pyfile")" < /dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        echo "python unittest exited with $rc for $pyfile" >&2
        fatal=$rc
    fi
done < /tmp/msb-pyfiles.txt

exit "$fatal"
"""


def _target_id(match: re.Match[str]) -> str:
    if not match.group("unit"):
        return match.group("target")
    stem = _BIN_HASH_RE.sub("", match.group("binary").rsplit("/", 1)[-1])
    source = match.group("target").rsplit("/", 1)[-1]
    if source == "lib.rs":
        return stem
    return f"{stem}-{source[:-3] if source.endswith('.rs') else source}"


def _doc_ids(entries: list[tuple[str, str, int, str]]) -> list[tuple[str, str]]:
    groups: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for path, name, line, status in entries:
        groups.setdefault((path, name), []).append((line, status))
    ids: list[tuple[str, str]] = []
    for (path, name), hits in groups.items():
        if len(hits) == 1:
            ids.append((f"{path}::{name}", hits[0][1]))
            continue
        for index, (_, status) in enumerate(sorted(hits), start=1):
            ids.append((f"{path}::{name}#{index}", status))
    return ids


def _py_name(method: str, qualified: str) -> str:
    parts = [part for part in qualified.split(".") if part]
    if parts and parts[-1] == method:
        parts = parts[:-1]
    if parts:
        return f"{parts[-1]}::{method}"
    return method


def _py_bucket(
    status: str, passed: set[str], failed: set[str], skipped: set[str]
) -> set[str]:
    if status == "ok":
        return passed
    if status.startswith("skipped"):
        return skipped
    return failed


def _script_header() -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        f"source {_TEST_ENV}\n"
        "\n"
        f"REPO_ROOT={_REPO_DIR}\n"
        f"CARGO_DIR={_CARGO_DIR}\n"
        "\n"
        'cd "$REPO_ROOT"\n'
    )


def _test_cmds() -> str:
    return _DERIVE_TARGETS_SH + "\n" + _TEST_BODY_SH


class Codex30953To25623ImageBase(Image):
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
        image_name = self.dependency()
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
    CI=true \\
    CARGO_HOME=/usr/local/cargo \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    RUSTFLAGS="-C debuginfo=0" \\
    RUST_BACKTRACE=1 \\
    RUST_MIN_STACK=8388608 \\
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

RUN --mount=type=secret,id=mitm_ca,required=0 \\
    if [ -f /run/secrets/mitm_ca ]; then \\
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\
    fi

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    bubblewrap \\
    clang \\
    cmake \\
    libcap-dev \\
    libclang-dev \\
    pkg-config \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Codex30953To25623ImageDefault(Image):
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
        return Codex30953To25623ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

REPO_ROOT={_REPO_DIR}
CARGO_DIR={_CARGO_DIR}

cd "$REPO_ROOT"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

toolchain=$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$CARGO_DIR/rust-toolchain.toml" | head -1)
case "$toolchain" in
    [0-9]*) ;;
    *) toolchain=$(cd / && rustc --version | sed -n 's/^rustc \\([0-9][0-9.]*\\).*/\\1/p') ;;
esac
[ -n "$toolchain" ] || {{ echo "prepare.sh: could not determine a toolchain version"; exit 1; }}

rustup toolchain install --profile minimal "$toolchain" || {{ echo "prepare.sh: rustup toolchain install failed"; exit 1; }}
rustup default "$toolchain" || {{ echo "prepare.sh: rustup default failed"; exit 1; }}
export RUSTUP_TOOLCHAIN="$toolchain"
rustc --version
cargo --version

printf 'export RUSTUP_TOOLCHAIN=%s\\n' "$toolchain" > {_TEST_ENV}

cd "$CARGO_DIR"
cargo fetch --locked || {{ echo "prepare.sh: cargo fetch failed"; exit 1; }}

cd "$REPO_ROOT"
{_DERIVE_TARGETS_SH}

cd "$CARGO_DIR"
while read -r pkg dir; do
    [ -n "$pkg" ] || continue
    echo "building tests for $pkg ($dir)"
    cargo test --offline -p "$pkg" --no-run < /dev/null || {{ echo "prepare.sh: offline test build failed for $pkg"; exit 1; }}
done < /tmp/msb-packages.txt

cargo metadata --offline --no-deps --format-version 1 > /dev/null || {{ echo "prepare.sh: offline metadata failed"; exit 1; }}
python3 -c 'import unittest' || {{ echo "prepare.sh: python unittest unavailable"; exit 1; }}

cd "$REPO_ROOT"

if [ ! -s /tmp/msb-packages.txt ] && [ ! -s /tmp/msb-pyfiles.txt ]; then
    echo "prepare.sh: no cargo package and no python test file derived from the test patch"
    exit 1
fi

echo "DEPS_OK"

""",
            ),
            File(
                ".",
                "run.sh",
                f"""{_script_header()}
{_test_cmds()}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{_script_header()}
git apply --whitespace=nowarn /home/test.patch

{_test_cmds()}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{_script_header()}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{_test_cmds()}""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_full_name()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
"""


@Instance.register("openai", "codex_30953_to_25623")
class CODEX_30953_TO_25623(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Codex30953To25623ImageDefault(self.pr, self._config)

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

        clean_log = _ANSI_RE.sub("", test_log)

        rust_buckets = {
            "ok": passed_tests,
            "FAILED": failed_tests,
            "ignored": skipped_tests,
        }

        crate = ""
        target = ""
        py_file = ""
        py_pending = ""
        doc_entries: list[tuple[str, str, int, str]] = []

        for line in clean_log.splitlines():
            line = line.strip()

            crate_match = _CRATE_RE.match(line)
            if crate_match:
                crate = crate_match.group(1)
                if crate == ".":
                    crate = ""
                target = ""
                py_file = ""
                py_pending = ""
                continue

            py_match = _PY_FILE_RE.match(line)
            if py_match:
                py_file = py_match.group(1)
                crate = ""
                target = ""
                py_pending = ""
                continue

            target_match = _TARGET_RE.match(line)
            if target_match:
                target = _target_id(target_match)
                continue

            if _DOCTESTS_RE.match(line):
                target = ""
                continue

            if py_file:
                py_result = _PY_RESULT_RE.match(line)
                if py_result:
                    name = _py_name(py_result.group(1), py_result.group(2))
                    _py_bucket(py_result.group(3), passed_tests, failed_tests, skipped_tests).add(
                        f"{py_file}::{name}"
                    )
                    py_pending = ""
                    continue

                py_head = _PY_HEAD_RE.match(line)
                if py_head:
                    py_pending = _py_name(py_head.group(1), py_head.group(2))
                    continue

                if py_pending:
                    py_status = _PY_STATUS_RE.match(line)
                    if py_status:
                        _py_bucket(
                            py_status.group(1), passed_tests, failed_tests, skipped_tests
                        ).add(f"{py_file}::{py_pending}")
                        py_pending = ""
                        continue

            result_match = _RESULT_RE.match(line)
            if not result_match:
                continue

            name = result_match.group(1).strip()
            status = result_match.group(2)

            path = target
            doc_match = _DOC_NAME_RE.match(name)
            if doc_match:
                path = doc_match.group(1)
                name = (doc_match.group(2) + doc_match.group(4)).strip() or "doctest"
                if crate and not path.startswith(f"{crate}/"):
                    path = f"{crate}/{path}"
                doc_entries.append((path, name, int(doc_match.group(3)), status))
                continue

            if crate and not path.startswith(f"{crate}/"):
                path = f"{crate}/{path}" if path else crate

            if not path:
                continue

            rust_buckets[status].add(f"{path}::{name}")

        for doc_id, doc_status in _doc_ids(doc_entries):
            rust_buckets[doc_status].add(doc_id)

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
