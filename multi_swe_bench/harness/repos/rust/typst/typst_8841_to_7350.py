from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_IMAGE = "rust:1.98.0-bookworm"
_BASE_TAG = "base-8841_to_7350"
_REPO_DIR = "/home/typst"
_TEST_ENV = "/home/typst_test_env"
_TEST_PACKAGE = "typst-tests"
_TEST_STAGES = "eval,pdf,pdftags,svg,html"
_BINARY_SUFFIXES = (
    "png",
    "jpg",
    "jpeg",
    "gif",
    "ico",
    "pdf",
    "ttf",
    "otf",
    "woff",
    "woff2",
)
_PATCH_EXCLUDES = " ".join(f"--exclude='*.{suffix}'" for suffix in _BINARY_SUFFIXES)
_EXCLUDED_SUFFIX_RE = "\\\\.(" + "|".join(_BINARY_SUFFIXES) + ")$"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RESULT_RE = re.compile(r"^(\u2705|\u274c) (.+?) \(([^()]+?\.typ)(?::(\d+))?\)(?: panicked)?$")

_PREFETCH_SH = """scratch=/tmp/msb-prefetch
rm -rf "$scratch"
mkdir -p "$scratch"
tar -cf - --exclude=.git --exclude=target . | (cd "$scratch" && tar -xf -)

python3 - "$scratch" /home/test.patch /home/fix.patch <<'PYEOF'
import os
import re
import sys

scratch = sys.argv[1]
targets = ("Cargo.toml", "Cargo.lock")


def hunks(body):
    out = []
    current = None
    remaining = 0
    for line in body.split("\\n"):
        head = re.match(r"^@@ -(\\d+)(?:,(\\d+))? \\+\\d+(?:,\\d+)? @@", line)
        if head:
            remaining = int(head.group(2) or 1)
            current = (int(head.group(1)), remaining, [])
            out.append(current)
            continue
        if current is None or remaining <= 0:
            continue
        marker = line[:1]
        if marker == "+":
            current[2].append(line)
        elif marker in (" ", "-"):
            current[2].append(line)
            remaining -= 1
        elif line == "":
            current[2].append(" ")
            remaining -= 1
        else:
            remaining = 0
    return out


def apply_file(path, body):
    full = os.path.join(scratch, path)
    if not os.path.isfile(full):
        return
    with open(full, encoding="utf-8") as handle:
        lines = handle.read().split("\\n")
    offset = 0
    for start, count, block in hunks(body):
        at = start - 1 + offset
        replacement = [item[1:] for item in block if item[:1] in (" ", "+")]
        lines[at:at + count] = replacement
        offset += len(replacement) - count
    with open(full, "w", encoding="utf-8") as handle:
        handle.write("\\n".join(lines))


for patch_path in sys.argv[2:]:
    if not os.path.isfile(patch_path):
        continue
    with open(patch_path, encoding="utf-8", errors="replace") as handle:
        patch = handle.read()
    for chunk in ("\\n" + patch).split("\\ndiff --git ")[1:]:
        header = re.match(r"a/(\\S+) b/(\\S+)", chunk)
        if not header:
            continue
        path = header.group(2)
        if os.path.basename(path) not in targets:
            continue
        if "\\n@@" not in chunk:
            continue
        apply_file(path, chunk)
        print("prefetch manifest:", path)
PYEOF

(cd "$scratch" && cargo fetch) || { echo "prepare.sh: patched manifest prefetch failed"; rm -rf "$scratch"; exit 1; }
rm -rf "$scratch"
"""

_DROP_EXCLUDED_DELETES_SH = f"""drop_excluded_deletes() {{
    awk -v pattern='{_EXCLUDED_SUFFIX_RE}' '
        /^diff --git a\\// {{
            if (deleted && path ~ pattern) print path
            path = $3; sub(/^a\\//, "", path); deleted = 0
            next
        }}
        /^deleted file mode / {{ deleted = 1 }}
        END {{ if (deleted && path ~ pattern) print path }}
    ' "$@" | while read -r victim; do
        [ -e "$victim" ] || continue
        rm -f "$victim"
        echo "removed excluded deletion: $victim"
    done
}}
"""

_TEST_BODY_SH = f"""fatal=0
rc=0

cargo test --offline -p {_TEST_PACKAGE} --no-fail-fast -- \\
    --stages {_TEST_STAGES} --verbose --no-report < /dev/null \\
    > /tmp/msb-cargo.log 2>&1 || rc=$?
cat /tmp/msb-cargo.log

if [ "$rc" -ne 0 ] && [ "$rc" -ne 101 ]; then
    echo "cargo test exited with $rc" >&2
    fatal=$rc
fi

if grep -qE '^error: could not compile' /tmp/msb-cargo.log; then
    echo "cargo test failed to build {_TEST_PACKAGE}" >&2
    fatal=1
fi

if grep -q 'failed to collect tests' /tmp/msb-cargo.log; then
    echo "test collection failed" >&2
    fatal=1
fi

exit "$fatal"
"""


def _script_header() -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "\n"
        "export CI=true\n"
        f"source {_TEST_ENV}\n"
        "\n"
        f"REPO_ROOT={_REPO_DIR}\n"
        "\n"
        'cd "$REPO_ROOT"\n'
    )


class Typst8841To7350ImageBase(Image):
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
    clang \\
    cmake \\
    libclang-dev \\
    pkg-config \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Typst8841To7350ImageDefault(Image):
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
        return Typst8841To7350ImageBase(self.pr, self._config)

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

cd "$REPO_ROOT"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

toolchain=$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' rust-toolchain.toml 2>/dev/null | head -1)
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

msrv=$(sed -n 's/^[[:space:]]*rust-version[[:space:]]*=[[:space:]]*"\\([^"]*\\)".*/\\1/p' Cargo.toml | head -1)
echo "workspace rust-version: $msrv"
installed=$(rustc --version | sed -n 's/^rustc \\([0-9.]*\\).*/\\1/p')
[ -n "$msrv" ] || {{ echo "prepare.sh: workspace rust-version not found"; exit 1; }}
printf '%s\\n%s\\n' "$msrv" "$installed" | sort -V -C || {{ echo "prepare.sh: toolchain $installed is older than workspace rust-version $msrv"; exit 1; }}

printf 'export RUSTUP_TOOLCHAIN=%s\\n' "$toolchain" > {_TEST_ENV}

cargo fetch --locked || {{ echo "prepare.sh: cargo fetch failed"; exit 1; }}

{_PREFETCH_SH}

cargo test --offline -p {_TEST_PACKAGE} --no-run < /dev/null || {{ echo "prepare.sh: offline test build failed"; exit 1; }}

cargo metadata --offline --no-deps --format-version 1 > /dev/null || {{ echo "prepare.sh: offline metadata failed"; exit 1; }}

echo "DEPS_OK"

""",
            ),
            File(
                ".",
                "run.sh",
                f"""{_script_header()}
{_TEST_BODY_SH}""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{_script_header()}
git apply --whitespace=nowarn {_PATCH_EXCLUDES} /home/test.patch

{_DROP_EXCLUDED_DELETES_SH}
drop_excluded_deletes /home/test.patch

{_TEST_BODY_SH}""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{_script_header()}
git apply --whitespace=nowarn {_PATCH_EXCLUDES} /home/test.patch /home/fix.patch

{_DROP_EXCLUDED_DELETES_SH}
drop_excluded_deletes /home/test.patch /home/fix.patch

{_TEST_BODY_SH}""",
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


@Instance.register("typst", "typst_8841_to_7350")
class TYPST_8841_TO_7350(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Typst8841To7350ImageDefault(self.pr, self._config)

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

        for line in clean_log.splitlines():
            line = line.strip()

            result_match = _RESULT_RE.match(line)
            if not result_match:
                continue

            marker = result_match.group(1)
            name = result_match.group(2).strip()
            path = result_match.group(3).strip()

            if not name or not path:
                continue

            test_id = f"{path}::{name}"
            if marker == "\u2705":
                passed_tests.add(test_id)
            else:
                failed_tests.add(test_id)

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
