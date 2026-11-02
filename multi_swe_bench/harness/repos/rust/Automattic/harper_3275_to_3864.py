import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

RUST_IMAGE = "rust:1.97-bookworm"
RUST_CHANNEL = "1.97"
NODE_VERSION = "24.15.0"
WASM_PACK_VERSION = "0.15.0"
BASE_TAG = "base-3275_to_3864"

_ENV = f"""\
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_PROFILE_DEV_DEBUG=0
export RUSTUP_MAX_RETRIES=10
export RUSTUP_TOOLCHAIN={RUST_CHANNEL}
export RUST_BACKTRACE=1
"""

_JS_TEST_FILE = re.compile(
    r"^(.+?)/((?:src|tests?|__tests__)/(?:.+/)?[^/]+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|cts))$"
)
_RUST_DIRS = ("src", "tests", "benches", "examples")


def _patch_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        m = re.match(r"^(?:\+\+\+ b|--- a)/(.+?)\t?$", line)
        if m and m.group(1) not in paths:
            paths.append(m.group(1))
    return paths


def _js_suites(test_patch: str) -> list[tuple[str, list[str]]]:
    suites: dict[str, set[str]] = {}
    for path in _patch_paths(test_patch):
        m = _JS_TEST_FILE.match(path)
        if m:
            suites.setdefault(m.group(1), set()).add(m.group(2))
    return [(pkg, sorted(files)) for pkg, files in sorted(suites.items())]


def _rust_crates(test_patch: str) -> list[str]:
    crates = set()
    for path in _patch_paths(test_patch):
        if _JS_TEST_FILE.match(path) or path.startswith("packages/"):
            continue
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in _RUST_DIRS:
                if i > 0:
                    crates.add("/".join(parts[:i]))
                break
    return sorted(crates)


def _q(value: str) -> str:
    return shlex.quote(value)


def _manifest(crate: str) -> str:
    return _q(f"{crate}/Cargo.toml")


_WASM_BUILD = r"""if [ -f harper-wasm/Cargo.toml ]; then
  (cd harper-wasm && wasm-pack build --target web --no-opt --out-name harper_wasm && wasm-pack build --target web --no-opt --out-name harper_wasm_slim --no-default-features)
  perl -pi -e 's/new URL\(.*\)/new URL()/g' harper-wasm/pkg/harper_wasm.js harper-wasm/pkg/harper_wasm_slim.js
fi"""

_WASM_TEST = r"""if [ -f harper-wasm/Cargo.toml ]; then
  { (cd harper-wasm && wasm-pack build --target web --no-opt --out-name harper_wasm && wasm-pack build --target web --no-opt --out-name harper_wasm_slim --no-default-features) && perl -pi -e 's/new URL\(.*\)/new URL()/g' harper-wasm/pkg/harper_wasm.js harper-wasm/pkg/harper_wasm_slim.js; } 2>&1 || RC=$?
fi"""


def _js_setup_cmd(test_patch: str) -> str:
    suites = _js_suites(test_patch)
    if not suites:
        return ""
    lines = [
        _WASM_BUILD,
        """PNPM_VERSION="$(node -p "((require('./package.json').packageManager || '').match(/^pnpm@([^+]+)/) || [])[1] || ''")\"""",
        'test -n "$PNPM_VERSION"',
        'npm install -g "pnpm@${PNPM_VERSION}"',
        "pnpm --version",
        "pnpm install --frozen-lockfile",
    ]
    for pkg, _ in suites:
        lines.append(
            f"if [ -x {_q(pkg)}/node_modules/.bin/playwright ]; then\n"
            f"  (cd {_q(pkg)} && pnpm exec playwright install --with-deps chromium firefox)\n"
            f"fi"
        )
    return "\n".join(lines)


def _gate_cmd(test_patch: str) -> str:
    lines = []
    for crate in _rust_crates(test_patch):
        lines.append(
            f"if [ -f {_manifest(crate)} ]; then\n"
            f"  cargo test --manifest-path {_manifest(crate)} --no-run\n"
            f"fi"
        )
    js_suites = _js_suites(test_patch)
    if js_suites:
        lines.append(
            "if [ -f harper-wasm/Cargo.toml ]; then\n"
            "  test -s harper-wasm/pkg/harper_wasm_bg.wasm\n"
            "  test -s harper-wasm/pkg/harper_wasm_slim_bg.wasm\n"
            "fi"
        )
    for pkg, _ in js_suites:
        lines.append(f"(cd {_q(pkg)} && pnpm exec vitest --version)")
    lines.append("echo DEPS_OK")
    return "\n".join(lines)


def _test_cmd(test_patch: str) -> str:
    blocks = []
    for crate in _rust_crates(test_patch):
        blocks.append(
            f"if [ -f {_manifest(crate)} ]; then\n"
            f"  echo {_q('MSB_SUITE|' + crate)}\n"
            f"  cargo test --manifest-path {_manifest(crate)} --no-fail-fast 2>&1 || RC=$?\n"
            f"fi"
        )
    js_suites = _js_suites(test_patch)
    if js_suites:
        blocks.append(_WASM_TEST)
    for pkg, files in js_suites:
        blocks.append(
            f"""\
JS_FILES=()
for f in {" ".join(_q(f) for f in files)}; do
  if [ -f {_q(pkg)}/"$f" ]; then JS_FILES+=("$f"); fi
done
if [ "${{#JS_FILES[@]}}" -gt 0 ]; then
  echo {_q('MSB_SUITE|vitest:' + pkg)}
  (cd {_q(pkg)} && pnpm exec vitest run --reporter=verbose "${{JS_FILES[@]}}") 2>&1 || RC=$?
fi"""
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


class Harper3275To3864ImageBase(Image):
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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_NET_RETRY=10 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    RUSTUP_MAX_RETRIES=10 \\
    RUSTUP_TOOLCHAIN={RUST_CHANNEL} \\
    RUST_BACKTRACE=1 \\
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
        git ca-certificates build-essential pkg-config curl xz-utils perl \\
        libssl-dev libgtk-3-dev librsvg2-dev libayatana-appindicator3-dev \\
        libwebkit2gtk-4.1-dev libsoup-3.0-dev libxdo-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64; rust_arch=x86_64 ;; \\
        arm64) node_arch=arm64; rust_arch=aarch64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" -o /tmp/node.tar.xz; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \\
    rm -f /tmp/node.tar.xz; \\
    curl -fsSL "https://github.com/wasm-bindgen/wasm-pack/releases/download/v{WASM_PACK_VERSION}/wasm-pack-v{WASM_PACK_VERSION}-$rust_arch-unknown-linux-musl.tar.gz" -o /tmp/wasm-pack.tar.gz; \\
    tar -xzf /tmp/wasm-pack.tar.gz -C /usr/local/bin --strip-components=1 --no-same-owner --wildcards '*/wasm-pack'; \\
    rm -f /tmp/wasm-pack.tar.gz; \\
    node --version; \\
    npm --version; \\
    wasm-pack --version

RUN rustup toolchain install {RUST_CHANNEL} --profile minimal --component rustfmt --component clippy --target wasm32-unknown-unknown && \\
    rustup default {RUST_CHANNEL} && \\
    rustc --version && cargo --version

RUN git config --global --add safe.directory '*'

{code}

CMD ["/bin/bash"]
"""


class Harper3275To3864ImageDefault(Image):
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
        return Harper3275To3864ImageBase(self.pr, self._config)

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
rustc --version
cargo --version
wasm-pack --version
node --version
cargo fetch --locked
JS_SETUP

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
                .replace("JS_SETUP", _js_setup_cmd(self.pr.test_patch))
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


@Instance.register("Automattic", "harper_3275_to_3864")
class HARPER_3275_TO_3864(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Harper3275To3864ImageDefault(self.pr, self._config)

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
        rust_buckets = {"ok": passed_tests, "FAILED": failed_tests, "ignored": skipped_tests}
        js_buckets = {"✓": passed_tests, "×": failed_tests, "↓": skipped_tests}

        re_suite = re.compile(r"^MSB_SUITE\|(\S+)\s*$")
        re_target = re.compile(r"^Running (?:unittests )?(\S+) \(")
        re_doc = re.compile(r"^Doc-tests (\S+)\s*$")
        re_rust = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)\b")
        re_doc_line = re.compile(r"\s+\(line \d+\)$")
        re_js = re.compile(
            r"^(✓|×|↓)\s+(.+? > .+?)(?:\s+\(retry x\d+\))?(?:\s+\d+(?:\.\d+)?\s?m?s)?(?:\s+\[[^\]]*\])?$"
        )
        re_output_start = re.compile(r"^---- .+ std(out|err) ----$")
        re_output_end = re.compile(r"^(test result:|running \d+ tests?$)")

        suite = ""
        target = ""
        in_output = False
        for line in log.splitlines():
            line = line.strip()
            m = re_suite.match(line)
            if m:
                suite = m.group(1)
                target = ""
                in_output = False
                continue
            if not suite:
                continue
            if suite.startswith("vitest:"):
                m = re_js.match(line)
                if m:
                    js_buckets[m.group(1)].add(f"{suite}::{m.group(2)}")
                continue
            m = re_target.match(line)
            if m:
                target = m.group(1)
                in_output = False
                continue
            m = re_doc.match(line)
            if m:
                target = "doc"
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
            m = re_rust.match(line)
            if m:
                name = re_doc_line.sub("", m.group(1)) if target == "doc" else m.group(1)
                rust_buckets[m.group(2)].add(f"{suite}:{target}::{name}")

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
