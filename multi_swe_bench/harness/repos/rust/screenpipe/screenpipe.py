import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.84-bookworm"
TEST_CMD = "cargo test -p screenpipe-server --test db -- --test-threads=1"
GATE_CMD = "cargo test -p screenpipe-server --test db --no-run\necho DEPS_OK"
GIT_DEP_PINS = [
    ("ffmpeg-sidecar", "92f3099d573e1ba57827865f92eb0ada91225737"),
    ("cpal", "e851e41b6e4f2318455b109b14dd5a64db3ac47a"),
    ("knf-rs", "b782e6bb02db495ce767f0094c0aa6bca5dba61c"),
    ("rusty-tesseract", "08346c1de08d122c7121425abc79081c30dbf778"),
    ("hf-hub", "55f6c592d7ff60b5a26676bf9c92acbca918f3aa"),
]
# No Cargo.lock is committed, so generate-lockfile resolves against today's index.
# These registry crates resolve to releases that need edition2024 (Cargo 1.85+);
# pin them back to late-2024 releases that cargo 1.84 can parse.
# ignore must drop before globset: the resolved ignore 0.4.29 requires globset ^0.4.18.
CRATE_PINS = [
    ("coreaudio-sys", "0.2.16"),
    ("subprocess", "0.2.9"),
    ("ignore", "0.4.23"),
    ("globset", "0.4.15"),
    # half >= 2.5 moved to rand 0.9 / rand_distr 0.5; candle-core 0.7.2 is on rand 0.8,
    # so StandardNormal: Distribution<f16> no longer holds (E0277 in cpu_backend).
    ("half", "2.4.1"),
]
# screenpipe-audio pins ort-sys =2.0.0-rc.8, whose build script downloads a prebuilt
# ONNX Runtime with its own HTTP client and panics on any transient network error.
# Pre-seed the exact cache dir that build script checks (dirs.rs cache_dir() +
# dfbin/<target>/<sha256>/onnxruntime), fetched with curl retries and verified
# against the same SHA-256 the build script asserts. URL + hash per target come from
# ort-sys-2.0.0-rc.8/dist.txt (feature set "none"), so amd64 and arm64 images each
# seed their own triple.
ORT_DIST = {
    "x86_64": (
        "https://parcel.pyke.io/v2/delivery/ortrs/packages/msort-binary/1.19.2/ortrs_static-v1.19.2-x86_64-unknown-linux-gnu.tgz",
        "c718efe9677e093bcd986a27ebe83bddd0dafd6967fafb0de311a239d27f4a71",
    ),
    "aarch64": (
        "https://parcel.pyke.io/v2/delivery/ortrs/packages/msort-binary/1.19.2/ortrs_static-v1.19.2-aarch64-unknown-linux-gnu.tgz",
        "b40ab3ea5bf3bcbc73c18ba66a8f96ecaa9d08963c0d6a5268d608b0642d23d4",
    ),
}
ORT_CMD = (
    'case "$(uname -m)" in\n'
    + "".join(
        '  {arch}) ORT_URL="{url}"; ORT_SHA256="{sha}" ;;\n'.format(arch=arch, url=url, sha=sha)
        for arch, (url, sha) in ORT_DIST.items()
    )
    + '  *) echo "unsupported arch for ort-sys prebuilt: $(uname -m)" >&2; exit 1 ;;\n'
    + "esac\n"
    + """ORT_SHA256_UPPER="$(echo "$ORT_SHA256" | tr a-f A-F)"
ORT_CACHE="${HOME:-/root}/.cache/ort.pyke.io/dfbin/$(uname -m)-unknown-linux-gnu/$ORT_SHA256_UPPER"
if [ ! -f "$ORT_CACHE/onnxruntime/lib/libonnxruntime.a" ]; then
  mkdir -p "$ORT_CACHE"
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/ort.tgz "$ORT_URL"
  echo "$ORT_SHA256  /tmp/ort.tgz" | sha256sum -c -
  tar -xzf /tmp/ort.tgz -C "$ORT_CACHE"
  rm -f /tmp/ort.tgz
fi
test -f "$ORT_CACHE/onnxruntime/lib/libonnxruntime.a\""""
)
LOCK_CMD = "\n".join(
    ["cargo generate-lockfile"]
    + [
        "cargo update -p {name} --precise {rev}".format(name=name, rev=rev)
        for name, rev in GIT_DEP_PINS + CRATE_PINS
    ]
)

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM {base_image}

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

# Rust flags live in cargo's global config, not ENV RUSTFLAGS: an env RUSTFLAGS
# overrides every [target.<triple>] entry, and aarch64 additionally needs +fp16 or
# gemm-f16 0.17.1 (candle-core dep) fails with "instruction requires: fullfp16".
# A [target] entry replaces [build].rustflags, so the link-arg is repeated there.
RUN printf '%s\\n' \\
        '[build]' \\
        'rustflags = ["-C", "link-arg=-Wl,--allow-multiple-definition"]' \\
        '' \\
        '[target.aarch64-unknown-linux-gnu]' \\
        'rustflags = ["-C", "link-arg=-Wl,--allow-multiple-definition", "-C", "target-feature=+fp16"]' \\
        > "${{CARGO_HOME}}/config.toml" && \\
    cat "${{CARGO_HOME}}/config.toml"
ENV CARGO_TERM_COLOR=never
ENV CARGO_NET_RETRY=5
ENV RUST_BACKTRACE=1
ENV CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS=fallback

WORKDIR /home/

RUN set -eux; \\
    for i in 1 2 3; do \\
        apt-get update && \\
        apt-get install -y --no-install-recommends --fix-missing \\
            ca-certificates curl unzip git build-essential pkg-config cmake clang libclang-dev \\
            libssl-dev libasound2-dev libxdo-dev libdbus-1-dev \\
            libxcb1-dev libxcb-render0-dev libxcb-shape0-dev libxcb-xfixes0-dev libxcb-randr0-dev \\
            libx11-dev libxi-dev libxext-dev libxtst-dev libxrandr-dev libxinerama-dev libxcursor-dev \\
            ffmpeg libavcodec-dev libavformat-dev libavutil-dev \\
            libavfilter-dev libavdevice-dev libswscale-dev \\
            tesseract-ocr libtesseract-dev libleptonica-dev \\
        && break || {{ echo "apt attempt $i failed, retrying"; sleep 10; }}; \\
    done; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""

GIT_HARDENING = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

# Upstream bug at base 9377333: migrations 20241110041538 and 20241126220600 only
# SELECT the `ALTER TABLE ... ADD COLUMN speaker_id` text and never execute it, so a
# fresh test DB has no audio_transcriptions.speaker_id and every audio test panics
# in all three stages. Neither patch touches these files. The run scripts start with
# `git reset --hard`, so each one re-applies this after reset/apply, identically.
# The later "ensure" migration becomes a no-op because SQLite ADD COLUMN is not
# idempotent.
FIX_SPEAKER_MIGRATION_SH = """#!/bin/bash
set -e
M=/home/[[REPO]]/screenpipe-server/src/migrations
test -f "$M/20241110041538_add_speaker_id_to_transcription.sql"
test -f "$M/20241126220600_ensure_speaker_id_column.sql"
printf '%s\\n' 'ALTER TABLE audio_transcriptions ADD COLUMN speaker_id INTEGER REFERENCES speakers(id);' \\
  > "$M/20241110041538_add_speaker_id_to_transcription.sql"
printf '%s\\n' 'SELECT 1;' > "$M/20241126220600_ensure_speaker_id_column.sql"
"""

PREPARE_SH = """#!/bin/bash
set -e
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

rustc --version
cargo --version

[[ORT]]

[[LOCK]]
cargo fetch

[[GATE]]
"""

RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
bash /home/fix_speaker_migration.sh

[[TEST_CMD]]
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/fix_speaker_migration.sh

[[TEST_CMD]]
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
git reset --hard
git clean -fd
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/fix_speaker_migration.sh

[[TEST_CMD]]
"""


def render_script(template: str, repo: str, sha: str = "") -> str:
    return (
        template.replace("[[REPO]]", repo)
        .replace("[[SHA]]", sha)
        .replace("[[ORT]]", ORT_CMD)
        .replace("[[LOCK]]", LOCK_CMD)
        .replace("[[GATE]]", GATE_CMD)
        .replace("[[TEST_CMD]]", TEST_CMD)
    )


class ScreenpipeImageBase(Image):
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
        return "base-screenpipe_790_to_790"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE.format(
            base_image=RUST_IMAGE,
            org=self.pr.org,
            repo=self.pr.repo,
        )


class ScreenpipeImageDefault(Image):
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
        return ScreenpipeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "fix_speaker_migration.sh", render_script(FIX_SPEAKER_MIGRATION_SH, repo)),
            File(".", "prepare.sh", render_script(PREPARE_SH, repo, sha)),
            File(".", "run.sh", render_script(RUN_SH, repo)),
            File(".", "test-run.sh", render_script(TEST_RUN_SH, repo)),
            File(".", "fix-run.sh", render_script(FIX_RUN_SH, repo)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        sections = ["FROM {name}:{tag}".format(name=name, tag=tag)]

        if self.global_env:
            sections.append(self.global_env)

        sections.append('ARG BASE_COMMIT="{sha}"'.format(sha=self.pr.base.sha))
        sections.append(copy_commands.strip())
        sections.append("RUN bash /home/prepare.sh")
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(GIT_HARDENING.strip())

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("screenpipe", "screenpipe")
class Screenpipe(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScreenpipeImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        status_re = re.compile(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)\s*$")

        for line in clean_log.splitlines():
            match = status_re.match(line.strip())
            if not match:
                continue

            name, status = match.group(1), match.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
