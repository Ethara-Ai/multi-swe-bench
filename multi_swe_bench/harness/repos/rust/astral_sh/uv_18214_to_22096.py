import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.98.1-bookworm"
BASE_TAG = "base-18214_to_22096"
CARGO_PROFILE = "fast-build"

_ENV = """\
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export RUSTUP_MAX_RETRIES=10
export RUST_BACKTRACE=1
export INSTA_UPDATE=no
export UV_HTTP_RETRIES=5
"""

_TEST_ATTR = re.compile(r"^\+.*#\[(tokio::test|test|rstest|test_case)\b", re.M)


def _patch_sections(patch: str) -> list[tuple[str, str]]:
    sections = []
    for section in re.split(r"(?m)^(?=diff --git )", patch):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        if m:
            sections.append((m.group(2), section))
    return sections


def _suites(test_patch: str) -> list[tuple[str, str, str, list[str]]]:
    suites: dict[tuple[str, str, str], set[str]] = {}
    for path, section in _patch_sections(test_patch):
        parts = path.split("/")
        if len(parts) < 4 or parts[0] != "crates":
            continue
        crate = parts[1]
        if parts[2] == "tests":
            if len(parts) == 4 and parts[3].endswith(".rs"):
                suites.setdefault((crate, "test", parts[3][:-3]), set())
            elif len(parts) >= 5:
                binary = parts[3]
                filters = suites.setdefault((crate, "test", binary), set())
                leaf = parts[-1]
                if leaf.endswith(".rs") and leaf not in ("main.rs", "mod.rs"):
                    filters.add(f"{leaf[:-3]}::")
                elif leaf == "mod.rs" and len(parts) >= 6:
                    filters.add(f"{parts[-2]}::")
                elif leaf.endswith(".snap"):
                    pieces = leaf.split("__")
                    if len(pieces) >= 3:
                        filters.add(f"{pieces[1]}::")
        elif parts[2] == "src":
            if path.endswith(".snap") or _TEST_ATTR.search(section):
                suites.setdefault((crate, "lib", "lib"), set())
    return [
        (crate, kind, target, sorted(filters))
        for (crate, kind, target), filters in sorted(suites.items())
    ]


def _quoted(items: list[str]) -> str:
    return " ".join(f"'{i}'" for i in items)


def _suite_guard(crate: str, kind: str, target: str) -> str:
    if kind == "lib":
        return f"[ -f 'crates/{crate}/Cargo.toml' ] && [ -f 'crates/{crate}/src/lib.rs' ]"
    return (
        f"{{ [ -f 'crates/{crate}/tests/{target}/main.rs' ] || "
        f"[ -f 'crates/{crate}/tests/{target}.rs' ]; }}"
    )


def _suite_target(kind: str, target: str) -> str:
    return "--lib" if kind == "lib" else f"--test '{target}'"


def _gate_cmd(test_patch: str) -> str:
    lines = []
    for crate, kind, target, _ in _suites(test_patch):
        lines.append(
            f"if {_suite_guard(crate, kind, target)}; then\n"
            f"  cargo test --profile {CARGO_PROFILE} -p '{crate}' {_suite_target(kind, target)} --no-run\n"
            f"fi"
        )
    lines.append(
        f'PY_DIR="$(cargo run -q --profile {CARGO_PROFILE} -p uv --bin uv -- python dir)"'
    )
    lines.append('test -n "$(ls -A "$PY_DIR")"')
    lines.append(
        f"cargo run -q --profile {CARGO_PROFILE} -p uv --bin uv -- python list --only-installed"
    )
    lines.append("echo DEPS_OK")
    return "\n".join(lines)


def _test_cmd(test_patch: str) -> str:
    blocks = []
    for crate, kind, target, filters in _suites(test_patch):
        suite = f"{crate}/{target}"
        args = f" -- {_quoted(filters)}" if filters else ""
        blocks.append(
            f"if {_suite_guard(crate, kind, target)}; then\n"
            f"  echo 'MSB_SUITE|{suite}'\n"
            f"  cargo test --profile {CARGO_PROFILE} -p '{crate}' {_suite_target(kind, target)} --no-fail-fast{args} || RC=$?\n"
            f"fi"
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


class UvImageBase(Image):
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
    RUSTUP_MAX_RETRIES=10 \\
    RUST_BACKTRACE=1 \\
    INSTA_UPDATE=no \\
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
        git git-lfs ca-certificates build-essential cmake pkg-config curl python3 xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN git lfs install --system && rustup --version && cargo --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class UvImageDefault(Image):
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
        return UvImageBase(self.pr, self._config)

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
TOOLCHAIN="$(sed -n 's/^channel *= *"\\(.*\\)"/\\1/p' rust-toolchain.toml)"
test -n "$TOOLCHAIN"
rustup toolchain install "$TOOLCHAIN" --profile minimal
rustc --version
cargo fetch
cargo run --profile PROFILE -p uv --bin uv -- python install

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
                .replace("PROFILE", CARGO_PROFILE)
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


@Instance.register("astral-sh", "uv_18214_to_22096")
class UV_18214_TO_22096(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return UvImageDefault(self.pr, self._config)

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
        buckets = {"ok": passed_tests, "FAILED": failed_tests, "ignored": skipped_tests}

        re_suite = re.compile(r"^MSB_SUITE\|(\S+)\s*$")
        re_test = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
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
            if re_output_start.match(line):
                in_output = True
                continue
            if re_output_end.match(line):
                in_output = False
                continue
            if in_output:
                continue
            m = re_test.match(line)
            if m and suite:
                buckets[m.group(2)].add(f"{suite}::{m.group(1)}")

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
