from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.83-bookworm"

_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+) b/", re.M)
_CRATE_DIRS = ("lib", "sdk", "core")


def patch_files(patch: str) -> list[str]:
    return _DIFF_FILE_RE.findall(patch or "")


def integration_targets(pr: PullRequest) -> list[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for path in patch_files(pr.test_patch):
        parts = path.split("/")
        if not path.endswith(".rs"):
            continue
        if len(parts) >= 3 and parts[0] in _CRATE_DIRS and parts[1] == "tests":
            targets.add((parts[0], parts[-1][: -len(".rs")]))
        elif len(parts) == 2 and parts[0] == "tests":
            targets.add((".", parts[-1][: -len(".rs")]))
    return sorted(targets)


def unit_test_crates(pr: PullRequest) -> list[str]:
    crates: set[str] = set()
    for path in patch_files(pr.test_patch):
        parts = path.split("/")
        if not path.endswith(".rs"):
            continue
        if len(parts) >= 3 and parts[0] in _CRATE_DIRS and parts[1] == "src":
            crates.add(parts[0])
    return sorted(crates)


CHECKOUT = r"""RUN git reset --hard
RUN git checkout [[SHA]]"""

HARDENING = r"""RUN set -eux; \
    git checkout --detach "[[SHA]]"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "[[SHA]]")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN test -f .gitmodules && \
    git submodule foreach --recursive ' \
        git checkout --detach HEAD; \
        git remote remove origin 2>/dev/null || true; \
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
            | xargs -r -n1 git update-ref -d; \
        git reflog expire --expire=now --all; \
        git reflog expire --expire-unreachable=now --all; \
        git gc --prune=now --aggressive; \
        rm -f .git/objects/info/alternates; \
    ' || true"""

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

fix_time_crate() {
    tv=$(awk '/^name = "time"$/{f=1;next} f&&/^version/{gsub(/[",]/,"",$3); if ($3 ~ /^0[.]3[.]/) print $3; f=0}' Cargo.lock | sort -V | tail -1)
    test -n "$tv" || return 0
    oldest=$(printf '%s
0.3.35
' "$tv" | sort -V | head -1)
    if [ "$oldest" = "$tv" ] && [ "$tv" != "0.3.35" ]; then
        echo "time $tv predates the Rust 1.80 API change; updating"
        cargo update -p "time@$tv" --precise 0.3.36 || cargo update -p time --precise 0.3.36 || true
    fi
}

export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }--cfg surrealdb_unstable"

rustc --version
cargo --version

cargo fetch --locked || cargo fetch

fix_time_crate

[[PREBUILD]]

git checkout -- .
git clean -fdq -e target -e Cargo.lock
bash /home/check_git_changes.sh
"""

STAGE_SH = r"""#!/bin/bash
set -o pipefail
export CI=true
export TZ=UTC
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1
export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }--cfg surrealdb_unstable"

cd /home/[[REPO]] || exit 1

git checkout -- . 2>/dev/null || true
git clean -fdq -e target -e Cargo.lock 2>/dev/null || true

fix_time_crate() {
    tv=$(awk '/^name = "time"$/{f=1;next} f&&/^version/{gsub(/[",]/,"",$3); if ($3 ~ /^0[.]3[.]/) print $3; f=0}' Cargo.lock | sort -V | tail -1)
    test -n "$tv" || return 0
    oldest=$(printf '%s
0.3.35
' "$tv" | sort -V | head -1)
    if [ "$oldest" = "$tv" ] && [ "$tv" != "0.3.35" ]; then
        echo "time $tv predates the Rust 1.80 API change; updating"
        cargo update -p "time@$tv" --precise 0.3.36 || cargo update -p time --precise 0.3.36 || true
    fi
}

crate_features() {
    manifest="$1"
    shift
    picked=""
    for feat in "$@"; do
        if grep -qE "^${feat}[[:space:]]*=" "$manifest"; then
            picked="${picked:+$picked,}$feat"
        fi
    done
    echo "$picked"
}

fix_time_crate

[[PATCH_STEP]]
[[TEST_STEP]]
exit 0
"""

APPLY = r"""apply_patch() {
    test -s "$1" || { echo "apply_patch: $1 is empty or missing"; return 0; }
    git apply --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied cleanly"; return 0; }
    git apply --3way --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied via 3-way merge (SUSPECT)"; return 0; }
    git apply -C1 --recount --whitespace=nowarn "$1" 2>/dev/null && {
        echo "apply_patch: $1 -> applied with reduced context (SUSPECT)"; return 0; }
    patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch -r /dev/null -i "$1" >/dev/null 2>&1 && {
        echo "apply_patch: $1 -> applied with fuzz (SUSPECT)"; return 0; }
    echo "apply_patch: $1 -> DID NOT APPLY"
    return 0
}
"""


class SurrealdbIntervalImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return "base"

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

ENV CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    RUST_BACKTRACE=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential curl \\
    cmake clang llvm libclang-dev pkg-config libssl-dev protobuf-compiler \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global http.version HTTP/1.1 && \\
    git config --global http.postBuffer 524288000 && \\
    git config --global http.lowSpeedLimit 1000 && \\
    git config --global http.lowSpeedTime 600 && \\
    for attempt in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        git clone "${{REPO_URL}}" /home/{repo} && break; \\
        echo "clone attempt $attempt failed, retrying"; \\
        sleep 20; \\
    done && \\
    test -d /home/{repo}/.git

CMD ["/bin/bash"]
"""


class SurrealdbIntervalImageDefault(Image):
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
        return SurrealdbIntervalImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return template.replace("[[REPO]]", self.pr.repo).replace(
            "[[SHA]]", self.pr.base.sha
        )

    def _manifest(self, crate: str) -> str:
        return "Cargo.toml" if crate == "." else f"{crate}/Cargo.toml"

    def _wanted_features(self) -> str:
        wanted = ["kv-mem", "sql2"]
        if any(t == "script" for _, t in integration_targets(self.pr)):
            wanted.append("scripting")
        return " ".join(wanted)

    def _prebuild_block(self) -> str:
        lines = []
        for crate, target in integration_targets(self.pr):
            lines.append(
                f'cargo test --manifest-path {self._manifest(crate)} '
                f'--test {target} --no-run || true'
            )
        for crate in unit_test_crates(self.pr):
            lines.append(
                f'cargo test --manifest-path {self._manifest(crate)} '
                f'--lib --no-run || true'
            )
        if not lines:
            lines.append("cargo build --tests || true")
        return "\n".join(lines)

    def _test_step(self) -> str:
        lines = []
        for crate, target in integration_targets(self.pr):
            manifest = self._manifest(crate)
            lines.append(f'if [ -f "{manifest}" ]; then')
            lines.append(
                f'    cargo test --manifest-path {manifest} '
                f'--features "$(crate_features {manifest} {self._wanted_features()})" '
                f'--test {target} --no-fail-fast -- --test-threads=1'
            )
            lines.append("fi")
        for crate in unit_test_crates(self.pr):
            manifest = self._manifest(crate)
            lines.append(f'if [ -f "{manifest}" ]; then')
            lines.append(
                f'    cargo test --manifest-path {manifest} '
                f'--features "$(crate_features {manifest} {self._wanted_features()})" '
                f'--lib --no-fail-fast -- --test-threads=1'
            )
            lines.append("fi")
        return "\n".join(lines)

    def _stage(self, patch_step: str) -> str:
        body = STAGE_SH.replace("[[PATCH_STEP]]", patch_step)
        body = body.replace("[[TEST_STEP]]", self._test_step())
        return self._expand(body)

    def install_files(self) -> list[File]:
        prepare = PREPARE_SH.replace("[[PREBUILD]]", self._prebuild_block())
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(prepare)),
        ]

    def grading_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", self._stage("")),
            File(
                ".",
                "test-run.sh",
                self._stage(APPLY + "apply_patch /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._stage(
                    APPLY
                    + "apply_patch /home/test.patch\napply_patch /home/fix.patch\n"
                ),
            ),
        ]

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

{self._expand(CHECKOUT)}

{copies(self.install_files())}
{copies(self.grading_files())}
RUN bash /home/prepare.sh

{self._expand(HARDENING)}

{self.clear_env}
"""


@Instance.register("surrealdb", "surrealdb_4743_to_2624")
class Surrealdb4743To2624(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SurrealdbIntervalImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        result_re = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)\s*$", re.M)
        bucket = {"ok": passed, "FAILED": failed, "ignored": skipped}
        for name, status in result_re.findall(log):
            bucket[status].add(name.strip())

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
