import json
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base"
_RUST_IMAGE = "rust:1.99.1-trixie"
_RUST_VERSION = "1.99.1"
_NODE_VERSION = "24.22.1"
_BUN_VERSION = "1.3.13"
_USER = "tester"
_CHROME = "/usr/local/bin/chromium-no-sandbox"
_NODE_REPORTER = "/home/node_test_events.mjs"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BINARY_DIFF = re.compile(r"^Binary files (?:a/(.+?)|/dev/null) and (?:b/(.+?)|/dev/null) differ$", re.M)
_GLOB_SPECIAL = re.compile(r"([*?\[\\])")
_BUN_FILE = re.compile(r"^(\S+\.(?:test|spec)\.[cm]?[jt]sx?):$")
_BUN_RESULT = re.compile(r"^\((pass|fail|skip|todo)\) (.+?)(?: \[\d+(?:\.\d+)?m?s\])?$")
_BUN_RESULTS = {"pass": "pass", "fail": "fail", "skip": "skip", "todo": "skip"}
_RESULTS = {"pass", "fail", "skip"}

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_NODE_TEST_EVENTS = """const ancestry = new Map();
const occurrences = new Map();

export default async function* reporter(source) {
  for await (const event of source) {
    const data = event.data ?? {};
    const file = data.file ?? "";
    if (event.type === "test:start") {
      const stack = ancestry.get(file) ?? [];
      stack.length = data.nesting;
      stack.push(data.name);
      ancestry.set(file, stack);
      continue;
    }
    if (event.type !== "test:pass" && event.type !== "test:fail") {
      continue;
    }
    if (data.details?.type === "suite") {
      continue;
    }
    const relative = file.startsWith(process.cwd() + "/") ? file.slice(process.cwd().length + 1) : file;
    const path = [relative, ...(ancestry.get(file) ?? []).slice(0, data.nesting), data.name].join(" > ");
    const occurrence = (occurrences.get(path) ?? 0) + 1;
    occurrences.set(path, occurrence);
    const name = occurrence === 1 ? path : `${path} #${occurrence}`;
    const skipped = data.skip !== undefined || data.todo !== undefined;
    const result = skipped ? "skip" : event.type === "test:pass" ? "pass" : "fail";
    yield JSON.stringify({ Case: name, Result: result }) + "\\n";
  }
}
"""

_CARGO_TEST_EVENTS = """import { spawnSync } from "node:child_process";
import path from "node:path";

const root = process.cwd();
const selection = ["--locked", "--workspace", "--lib", "--bins", "--tests"];
const build = spawnSync("cargo", ["test", ...selection, "--no-run", "--message-format=json"], {
  encoding: "utf8",
  maxBuffer: 1 << 30,
  stdio: ["ignore", "pipe", "inherit"],
});
let status = build.status === 0 ? 0 : 1;

const binaries = new Map();
for (const line of build.stdout.split("\\n")) {
  if (!line.startsWith("{")) {
    continue;
  }
  const message = JSON.parse(line);
  if (message.reason !== "compiler-artifact" || !message.executable || !message.profile?.test) {
    continue;
  }
  const manifestDir = path.dirname(message.manifest_path);
  const [name, version] = (message.package_id.split("#").pop() ?? "").split("@");
  binaries.set(path.relative(root, message.target.src_path), {
    executable: message.executable,
    manifestDir,
    manifestPath: message.manifest_path,
    name,
    version: version ?? "",
  });
}

const outcomes = { ok: "pass", FAILED: "fail", ignored: "skip" };
for (const source of [...binaries.keys()].sort()) {
  const binary = binaries.get(source);
  console.log(`$ ${source}`);
  const run = spawnSync(binary.executable, ["--test-threads=1"], {
    cwd: binary.manifestDir,
    encoding: "utf8",
    maxBuffer: 1 << 30,
    stdio: ["ignore", "pipe", "inherit"],
    env: {
      ...process.env,
      CARGO_MANIFEST_DIR: binary.manifestDir,
      CARGO_MANIFEST_PATH: binary.manifestPath,
      CARGO_PKG_NAME: binary.name,
      CARGO_PKG_VERSION: binary.version,
    },
  });
  if (run.status !== 0) {
    status = 1;
  }
  for (const line of (run.stdout ?? "").split("\\n")) {
    process.stdout.write(line + "\\n");
    const result = line.match(/^test (\\S+) \\.\\.\\. (ok|FAILED|ignored)\\b/);
    if (result) {
      console.log(JSON.stringify({ Case: `${source}::${result[1]}`, Result: outcomes[result[2]] }));
    }
  }
}

process.exit(status);
"""

_DEPENDENCIES_LINKED = (
    'const p = require("./package.json"); '
    "for (const d of Object.keys({ ...p.dependencies, ...p.devDependencies })) "
    'require("node:fs").statSync("node_modules/" + d);'
)


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _prepare_script(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

bun install --frozen-lockfile
if [ -f Cargo.toml ]; then
    cargo fetch --locked
    cargo build --locked --workspace --lib --bins --tests
    test -x target/debug/{repo}
fi
node -e '{_DEPENDENCIES_LINKED}'
"""


def _run_script(repo: str, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{repo}", apply) if line)
    return f"""#!/bin/bash
set -eo pipefail
export CI=true

{setup}

status=0
if [ -f Cargo.toml ]; then
    cargo build --locked -p {repo} || status=$?
    export IMPECCABLE_BIN=/home/{repo}/target/debug/{repo}
    node /home/cargo_test_events.mjs || status=$?
fi

export NODE_OPTIONS=--test-reporter={_NODE_REPORTER}
node scripts/run-tests.mjs default || status=$?

exit $status
"""


def _binary_paths(*patches: str) -> list[str]:
    found: dict[str, None] = {}
    for patch in patches:
        for old, new in _BINARY_DIFF.findall(patch):
            found[new or old] = None
    return list(found)


def _apply_command(excluded: list[str], *patch_files: str) -> str:
    flags = [
        "--exclude=" + shlex.quote(_GLOB_SPECIAL.sub(r"\\\1", path))
        for path in excluded
    ]
    return " ".join(["git apply --whitespace=nowarn", *flags, *patch_files])


def _test_event(line: str) -> tuple[str, str] | None:
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    case = event.get("Case")
    if isinstance(case, str) and case and event.get("Result") in _RESULTS:
        return case, event["Result"]
    return None


class ImpeccableImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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

ENV RUSTUP_TOOLCHAIN={_RUST_VERSION} \\
    CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    CARGO_BUILD_JOBS=4 \\
    RUST_BACKTRACE=1 \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CARGO_HOME=/home/{_USER}/.cargo \\
    CHROME_PATH={_CHROME} \\
    PUPPETEER_EXECUTABLE_PATH={_CHROME} \\
    PUPPETEER_SKIP_DOWNLOAD=true \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \\
    IMPECCABLE_TELEMETRY_CHANNEL=test

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
    git ca-certificates build-essential pkg-config libssl-dev unzip chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash {_USER} && \\
    printf '#!/bin/sh\\nexec /usr/bin/chromium --no-sandbox "$@"\\n' > {_CHROME} && \\
    chmod 0755 {_CHROME}

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64; bun_arch=x64-baseline ;; \\
        arm64) node_arch=arm64; bun_arch=aarch64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v{_NODE_VERSION}/node-v{_NODE_VERSION}-linux-${{node_arch}}.tar.gz" \\
        | tar -xz -C /usr/local --strip-components=1 --no-same-owner; \\
    curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v{_BUN_VERSION}/bun-linux-${{bun_arch}}.zip"; \\
    unzip -q /tmp/bun.zip -d /tmp/bun; \\
    install -m 0755 /tmp/bun/bun-linux-${{bun_arch}}/bun /usr/local/bin/bun; \\
    ln -sf /usr/local/bin/bun /usr/local/bin/bunx; \\
    rm -rf /tmp/bun /tmp/bun.zip; \\
    rustup target add --toolchain {_RUST_VERSION} wasm32-unknown-unknown; \\
    node --version; bun --version; cargo --version

RUN git config --system --add safe.directory '*'"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null && \\
    chown -R {_USER}:{_USER} /home/{repo}

USER {_USER}"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class ImpeccableImageDefault(Image):
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
        return ImpeccableImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_apply = _apply_command(
            _binary_paths(self.pr.test_patch),
            "/home/test.patch",
        )
        fix_apply = _apply_command(
            _binary_paths(self.pr.test_patch, self.pr.fix_patch),
            "/home/test.patch",
            "/home/fix.patch",
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(repo, self.pr.base.sha)),
            File(".", "node_test_events.mjs", _NODE_TEST_EVENTS),
            File(".", "cargo_test_events.mjs", _CARGO_TEST_EVENTS),
            File(".", "run.sh", _run_script(repo)),
            File(".", "test-run.sh", _run_script(repo, test_apply)),
            File(".", "fix-run.sh", _run_script(repo, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register("pbakaus", "impeccable")
class IMPECCABLE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImpeccableImageDefault(self.pr, self._config)

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
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        bun_file = ""
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            event = _test_event(line)
            if event:
                buckets[event[1]].add(event[0])
                continue
            header = _BUN_FILE.match(line)
            if header:
                bun_file = header.group(1)
                continue
            result = _BUN_RESULT.match(line)
            if result and bun_file:
                buckets[_BUN_RESULTS[result.group(1)]].add(f"{bun_file} > {result.group(2)}")
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
