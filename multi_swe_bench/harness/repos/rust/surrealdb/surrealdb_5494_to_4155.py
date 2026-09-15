from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.83-bookworm"

CARGO_LOW_MEMORY_ENV = "env CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0"

TEST_FEATURES = ("kv-mem", "storage-mem", "http", "scripting", "jwks")

FLAKY_TESTS = ("idx::ft::tests::concurrent_test",)

SKIP_FLAKY_ARGS = " ".join(f"--skip {name}" for name in FLAKY_TESTS)

_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+) b/", re.M)


def patch_files(patch: str) -> list[str]:
    return _DIFF_FILE_RE.findall(patch or "")


def rust_test_files(patch: str) -> list[str]:
    return sorted({p for p in patch_files(patch) if p.endswith(".rs")})


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: clean"
exit 0
"""

RESOLVE_TARGETS_SH = r"""crate_features() {
    picked=""
    for feat in """ + " ".join(TEST_FEATURES) + r"""; do
        if grep -qE "^${feat}[[:space:]]*=" "$1"; then
            picked="${picked:+$picked,}$feat"
        fi
    done
    echo "$picked"
}

feature_args() {
    feats=$(crate_features "$1")
    if [ -n "$feats" ]; then
        echo "--features $feats"
    fi
}

resolve_targets() {
    for f in "$@"; do
        d=$(dirname "$f")
        manifest=""
        walk="$d"
        while true; do
            if [ -f "$walk/Cargo.toml" ]; then
                manifest="$walk/Cargo.toml"
                break
            fi
            [ "$walk" = "." ] && break
            walk=$(dirname "$walk")
        done
        [ -z "$manifest" ] && manifest="Cargo.toml"
        case "$f" in
            */src/*)
                echo "LIB|$manifest"
                ;;
            *tests/*)
                rest="${f#*tests/}"
                name="${rest%%/*}"
                name="${name%.rs}"
                echo "TEST|$manifest|$name"
                ;;
        esac
    done | sort -u
}

readarray -t TARGET_FILES <<'TARGETLIST'
[[TARGETS]]
TARGETLIST
FILTERED_FILES=()
for f in "${TARGET_FILES[@]}"; do
    test -n "$f" && FILTERED_FILES+=("$f")
done
"""

RUN_TARGETS_BUILD_ONLY = r"""any_ran=0
while IFS='|' read -r kind manifest name; do
    test -n "$kind" || continue
    any_ran=1
    if [ "$kind" = "LIB" ]; then
        cargo test --manifest-path "$manifest" $(feature_args "$manifest") --lib --no-run || true
    else
        cargo test --manifest-path "$manifest" $(feature_args "$manifest") --test "$name" --no-run || true
    fi
done < <(resolve_targets "${FILTERED_FILES[@]}")

if [ "$any_ran" = "0" ]; then
    cargo build --tests || true
fi
"""

RUN_TARGETS_TEST = r"""any_ran=0
while IFS='|' read -r kind manifest name; do
    test -n "$kind" || continue
    any_ran=1
    if [ "$kind" = "LIB" ]; then
        cargo test --manifest-path "$manifest" $(feature_args "$manifest") --lib --no-fail-fast -- --test-threads=1 """ + SKIP_FLAKY_ARGS + r"""
    else
        cargo test --manifest-path "$manifest" $(feature_args "$manifest") --test "$name" --no-fail-fast -- --test-threads=1 """ + SKIP_FLAKY_ARGS + r"""
    fi
done < <(resolve_targets "${FILTERED_FILES[@]}")

if [ "$any_ran" = "0" ]; then
    cargo test --no-fail-fast -- --test-threads=1 """ + SKIP_FLAKY_ARGS + r"""
fi
exit 0
"""

FIX_TIME_CRATE_SH = r"""fix_time_crate() {
    tv=$(awk '/^name = "time"$/{f=1;next} f&&/^version/{gsub(/[",]/,"",$3); if ($3 ~ /^0[.]3[.]/) print $3; f=0}' Cargo.lock 2>/dev/null | sort -V | tail -1)
    test -n "$tv" || return 0
    oldest=$(printf '%s\n0.3.35\n' "$tv" | sort -V | head -1)
    if [ "$oldest" = "$tv" ] && [ "$tv" != "0.3.35" ]; then
        cargo update -p "time@$tv" --precise 0.3.36 2>/dev/null || cargo update -p time --precise 0.3.36 2>/dev/null || true
    fi
}
fix_time_crate
"""

PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

export RUSTFLAGS="${RUSTFLAGS:+$RUSTFLAGS }--cfg surrealdb_unstable"
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
rustc --version
cargo --version

cargo fetch --locked || cargo fetch

""" + FIX_TIME_CRATE_SH + RESOLVE_TARGETS_SH + RUN_TARGETS_BUILD_ONLY + r"""
git checkout -- .
git clean -fdq -e target -e Cargo.lock
cargo check --offline
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

""" + FIX_TIME_CRATE_SH + RESOLVE_TARGETS_SH + r"""
[[PATCH_STEP]]

""" + RUN_TARGETS_TEST

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
    test -z "$(git status --porcelain)"; \
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


class SurrealdbEraImageBase(Image):
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
        return "base-5494_to_4155"

    def workdir(self) -> str:
        return "base-5494_to_4155"

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


class SurrealdbEraImageDefault(Image):
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
        return SurrealdbEraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return template.replace("[[REPO]]", self.pr.repo).replace(
            "[[SHA]]", self.pr.base.sha
        )

    def _targets_block(self) -> str:
        return "\n".join(rust_test_files(self.pr.test_patch))

    def _fill_targets(self, body: str) -> str:
        body = body.replace("[[TARGETS]]", "[[TARGETS_PLACEHOLDER]]")
        body = self._expand(body)
        return body.replace("[[TARGETS_PLACEHOLDER]]", self._targets_block())

    def _stage(self, patch_step: str) -> str:
        body = STAGE_SH.replace("[[PATCH_STEP]]", patch_step)
        return self._fill_targets(body)

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._fill_targets(PREPARE_SH)),
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
        sha = self.pr.base.sha

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

{copies(self.install_files())}
{copies(self.grading_files())}
ARG BASE_COMMIT="{sha}"
RUN bash /home/prepare.sh

{self._expand(HARDENING)}

{self.clear_env}
"""


@Instance.register("surrealdb", "surrealdb_5494_to_4155")
class Surrealdb5494To4155(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SurrealdbEraImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or f"{CARGO_LOW_MEMORY_ENV} bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or f"{CARGO_LOW_MEMORY_ENV} bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or f"{CARGO_LOW_MEMORY_ENV} bash /home/fix-run.sh"

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
