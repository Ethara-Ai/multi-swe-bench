import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "crates_io_14437_to_14437"
BASE_TAG = f"base-{_INTERVAL}"
RUST_IMAGE = "rust:1.97.1-trixie"
NODE_VERSION = "24.19.0"
PNPM_VERSION = "11.20.0"
MSW_FILTER = "@crates-io/msw"

CHECK_GIT_CHANGES = """#!/bin/bash
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

ENV_BLOCK = """export CI=true
export INSTA_UPDATE=no
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export CARGO_BUILD_JOBS=2
export CARGO_NET_RETRY=10
export RUST_BACKTRACE=0
export TEST_DATABASE_URL=postgres://postgres:postgres@localhost/cargo_registry_test
export NODE_OPTIONS=--max-old-space-size=2048
"""

PG_START = """PG_VERSION="$(ls /etc/postgresql | sort -V | tail -1)"
pg_ctlcluster "${PG_VERSION}" main start
"""

RUST_TEST = "cargo test -p crates_io --test integration --locked"

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail
exec 2>&1

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
"""
    + PG_START
)

REINSTALL_BLOCK = """
changed="$(git status --porcelain --untracked-files=all)"
if printf '%s\\n' "${changed}" | grep -E '(package\\.json|pnpm-lock\\.yaml|pnpm-workspace\\.yaml)$' >/dev/null; then
    pnpm install --no-frozen-lockfile --filter "__MSW__..."
fi
"""

TEST_BLOCK = (
    """
status=0

echo "=== MSB_SUITE: rust"
"""
    + RUST_TEST
    + """ --no-fail-fast || status=1

echo "=== MSB_SUITE: msw"
pnpm --filter "__MSW__" exec vitest run --reporter=tap-flat --passWithNoTests || status=1

exit "${status}"
"""
)

PREPARE_SH = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

"""
    + PG_START
    + """su postgres -c "psql -v ON_ERROR_STOP=1 -c \\"ALTER USER postgres WITH PASSWORD 'postgres'\\""
su postgres -c "createdb --lc-collate=C --lc-ctype=C -T template0 cargo_registry_test"

rustc --version
cargo fetch --locked
"""
    + RUST_TEST
    + """ --no-run

pnpm install --frozen-lockfile --filter "__MSW__..."
pnpm --filter "__MSW__" exec vitest --version

pg_ctlcluster "${PG_VERSION}" main stop
echo "DEPS_OK"

"""
)

RUN_SH = RUN_HEADER + TEST_BLOCK

TEST_RUN_SH = (
    RUN_HEADER
    + "git apply --whitespace=nowarn /home/test.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)

FIX_RUN_SH = (
    RUN_HEADER
    + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)


class CratesIoImageBase_14437_TO_14437(Image):
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
        return RUST_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {RUST_IMAGE}

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
    CI=true \\
    CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=10 \\
    PATH=/opt/node/bin:$PATH \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (rust 1.97.1, postgresql, node {NODE_VERSION})" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \\
    done && \\
    apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    libpq-dev \\
    libssl-dev \\
    pkg-config \\
    postgresql \\
    postgresql-contrib \\
    xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "${{TARGETARCH:-amd64}}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported arch ${{TARGETARCH}}"; exit 1 ;; esac; \\
    curl -fsSL --retry 5 -o /tmp/node.tar.xz "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-${{arch}}.tar.xz"; \\
    mkdir -p /opt/node; \\
    tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1; \\
    rm -f /tmp/node.tar.xz; \\
    npm install -g pnpm@{PNPM_VERSION}; \\
    node --version; \\
    pnpm --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git -c core.longpaths=true clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class CratesIoImageDefault_14437_TO_14437(Image):
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
        return CratesIoImageBase_14437_TO_14437(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return (
            script.replace("__REPO__", self.pr.repo)
            .replace("__SHA__", self.pr.base.sha)
            .replace("__MSW__", MSW_FILTER)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(PREPARE_SH)),
            File(".", "run.sh", self._render(RUN_SH)),
            File(".", "test-run.sh", self._render(TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("rust-lang", "crates.io")
@Instance.register("rust-lang", _INTERVAL)
class CRATES_IO_14437_TO_14437(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CratesIoImageDefault_14437_TO_14437(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        suite_re = re.compile(r"^=== MSB_SUITE: (\w+)$")
        target_re = re.compile(r"^\s*Running (?:unittests )?(\S+)")
        cargo_re = re.compile(r"test (\S+(?: - should panic)?) \.\.\. (ok|FAILED|ignored)")
        tap_re = re.compile(r"^(?P<status>ok|not ok)\s+\d+\s+-\s+(?P<rest>.*)$")
        dir_re = re.compile(r"#\s*(SKIP|TODO)", re.IGNORECASE)
        time_re = re.compile(r"\s*#\s*time=[^\s#]*\s*$")

        suite = "rust"
        target = ""
        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()

            suite_match = suite_re.match(line.strip())
            if suite_match:
                suite = suite_match.group(1)
                target = ""
                continue

            if suite == "rust":
                target_match = target_re.match(line)
                if target_match:
                    target = target_match.group(1)
                    continue
                match = cargo_re.search(line)
                if not match:
                    continue
                name = f"rust :: {target} :: {match.group(1)}" if target else f"rust :: {match.group(1)}"
                status = match.group(2)
                if status == "ok":
                    passed_tests.add(name)
                elif status == "FAILED":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            match = tap_re.match(line.strip())
            if not match:
                continue
            rest = match.group("rest")
            directive = dir_re.search(rest)
            if directive:
                rest = rest[: directive.start()]
            rest = time_re.sub("", rest).rstrip()
            if not rest:
                continue
            name = f"{suite} :: {rest}"
            if directive:
                skipped_tests.add(name)
            elif match.group("status") == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

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
