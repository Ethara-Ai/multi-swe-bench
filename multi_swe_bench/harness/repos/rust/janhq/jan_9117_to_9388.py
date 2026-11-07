import re
import shlex
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL = "jan_9117_to_9388"
_RUST_IMAGE = "rust:1.98.0-trixie"
_NODE_IMAGE = "node:22.23.4-trixie"
_YARN_VERSION = "4.5.3"
_PYTEST_VERSION = "8.4.2"
_SUITE_BEGIN = "jan-suite-begin"
_SUITE_END = "jan-suite-end"
_JUNIT_BEGIN = "jan-junit-begin"
_JUNIT_END = "jan-junit-end"
_SUITE_TIMEOUT = 5400

_RUST_CRATE = re.compile(r"^(src-tauri(?:/(?:plugins/[^/]+|jan-cli|utils))?)/.+\.rs$")
_VITEST_FILE = re.compile(
    r"^(web-app|core|extensions/[^/]+|src-tauri/plugins/[^/]+)/(.+\.(?:test|spec)\.[cm]?[jt]sx?)$"
)
_NODE_FILE = re.compile(r"^packages/[^/]+/.+\.test\.[cm]?js$")
_PYTEST_FILE = re.compile(r"^(?:(.+)/)?tests/(test_[^/]+\.py)$")
_SKIPPED_CRATES = {"src-tauri/plugins/tauri-plugin-mlx"}
_CRATE_FLAGS = {
    "src-tauri": ("--no-default-features --features test-tauri", "--test-threads=1"),
    "src-tauri/jan-cli": ("--no-default-features --features cli", ""),
    "src-tauri/plugins/tauri-plugin-agent-tools": ("--no-default-features", ""),
}

_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export HUSKY=0
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_NET_GIT_FETCH_WITH_CLI=true
export CARGO_HTTP_TIMEOUT=60
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C linker-features=-lld"
export NODE_OPTIONS=--max-old-space-size=4096
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_ENABLE_TELEMETRY=0
export YARN_ENABLE_PROGRESS_BARS=false
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
export PYTHONDONTWRITEBYTECODE=1
export JAN_CLI_NO_UPDATE_CHECK=1
export PUPPETEER_SKIP_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export npm_config_yes=true"""

_COMMON_HELPERS = """host_triple() {
  rustc -vV | sed -n 's/^host: //p'
}

lock_flag() {
  if git ls-files --error-unmatch "$1/Cargo.lock" >/dev/null 2>&1; then
    echo --locked
  fi
}"""

_RUN_HELPERS = (
    _COMMON_HELPERS
    + f"""

JUNIT=/tmp/jan-junit.xml

emit_junit() {{
  echo "{_JUNIT_BEGIN}"
  if [ -f "$JUNIT" ]; then
    cat "$JUNIT"
    echo
  fi
  echo "{_JUNIT_END}"
}}

stub_resources() {{
  if [ -f scripts/stub-tauri-resources.mjs ]; then
    node scripts/stub-tauri-resources.mjs
  fi
}}

crate_tests() {{
  (cd "$1" && cargo metadata --format-version 1 --no-deps --offline) | python -c 'import json,sys
pkg=json.load(sys.stdin)["packages"][0]
for t in pkg["targets"]:
    if "test" in t["kind"]:
        print(t["name"])'
}}

crate_has_lib() {{
  (cd "$1" && cargo metadata --format-version 1 --no-deps --offline) | python -c 'import json,sys
pkg=json.load(sys.stdin)["packages"][0]
sys.exit(0 if any(k in ("lib","rlib","cdylib","staticlib","dylib") for t in pkg["targets"] for k in t["kind"]) else 1)'
}}

run_cargo() {{
  local crate="$1" flags="$2" extra="$3" locked status=0 name
  locked=$(lock_flag "$crate")
  stub_resources
  if crate_has_lib "$crate"; then
    (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test $locked $flags --no-fail-fast --lib -- $extra) || status=$?
  fi
  (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test $locked $flags --no-fail-fast --bins -- $extra) || status=$?
  for name in $(crate_tests "$crate"); do
    (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test $locked $flags --no-fail-fast --test "$name" -- $extra) || status=$?
  done
  return "$status"
}}

build_jan() {{
  local target
  stub_resources
  (cd src-tauri/jan-cli && timeout -k 60 {_SUITE_TIMEOUT} cargo build $(lock_flag src-tauri/jan-cli) --no-default-features --features cli --bin jan) || return 1
  target=$( (cd src-tauri/jan-cli && cargo metadata --format-version 1 --no-deps --offline) | python -c 'import json,sys;print(json.load(sys.stdin)["target_directory"])')
  export JAN_BIN="$target/debug/jan"
}}

refresh_js() {{
  local dir
  if [ -n "$(git status --porcelain --untracked-files=all -- core)" ]; then
    yarn build:core
    yarn build:extensions
  else
    for dir in extensions/*/; do
      dir=${{dir%/}}
      if [ -f "$dir/package.json" ] && [ -n "$(git status --porcelain --untracked-files=all -- "$dir")" ]; then
        (cd "$dir" && yarn build)
      fi
    done
  fi
  for dir in src-tauri/plugins/*/; do
    dir=${{dir%/}}
    if [ -f "$dir/package.json" ] && [ -d "$dir/guest-js" ] && [ -n "$(git status --porcelain --untracked-files=all -- "$dir/guest-js" "$dir/package.json")" ]; then
      (cd "$dir" && yarn build)
    fi
  done
}}

run_vitest() {{
  local project="$1" status=0
  shift
  rm -f "$JUNIT"
  (cd "$project" && timeout -k 60 {_SUITE_TIMEOUT} yarn vitest run --reporter=default --reporter=junit --outputFile.junit="$JUNIT" "$@") || status=$?
  emit_junit
  return "$status"
}}

run_node() {{
  local file="$1" status=0
  rm -f "$JUNIT"
  timeout -k 60 {_SUITE_TIMEOUT} node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="$JUNIT" "$file" || status=$?
  emit_junit
  return "$status"
}}

run_pytest() {{
  local root="$1" file="$2" status=0
  rm -f "$JUNIT"
  (cd "$root" && timeout -k 60 {_SUITE_TIMEOUT} python -m pytest -p no:cacheprovider -v --junitxml="$JUNIT" "$file") || status=$?
  emit_junit
  return "$status"
}}"""
)

_PREPARE_HELPERS = (
    _COMMON_HELPERS
    + """

fetch_crate() {
  local crate="$1" host locked attempt
  host=$(host_triple)
  locked=$(lock_flag "$crate")
  if [ -z "$locked" ] && [ ! -f "$crate/Cargo.lock" ]; then
    cp src-tauri/Cargo.lock "$crate/Cargo.lock"
  fi
  for attempt in 1 2 3 4 5; do
    if (cd "$crate" && cargo fetch $locked --target "$host"); then
      return 0
    fi
    sleep 30
  done
  return 1
}

gate_crate() {
  local crate="$1" host
  host=$(host_triple)
  (cd "$crate" && cargo metadata $(lock_flag "$crate") --offline --filter-platform "$host" --format-version 1 >/dev/null)
}

provision_js() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if yarn install && yarn build:tauri:plugin:api; then
      break
    fi
    if [ "$attempt" = 5 ]; then
      return 1
    fi
    sleep 30
  done
  yarn build:core
  yarn build:extensions
}"""
)


def _paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch, re.M)


def _suites(pr: PullRequest) -> list[tuple[str, str, tuple[str, ...]]]:
    crates: set[str] = set()
    vitest: dict[str, set[str]] = {}
    node: set[str] = set()
    pytest: set[tuple[str, str]] = set()
    for path in _paths(pr.test_patch) + _paths(pr.fix_patch):
        found = _RUST_CRATE.match(path)
        if found and found.group(1) not in _SKIPPED_CRATES:
            crates.add(found.group(1))
    for path in _paths(pr.test_patch):
        found = _VITEST_FILE.match(path)
        if found:
            vitest.setdefault(found.group(1), set()).add(found.group(2))
            continue
        if _NODE_FILE.match(path):
            node.add(path)
            continue
        found = _PYTEST_FILE.match(path)
        if found:
            pytest.add((found.group(1) or ".", f"tests/{found.group(2)}"))
    suites: list[tuple[str, str, tuple[str, ...]]] = []
    for crate in sorted(crates):
        suites.append(("cargo", crate, ()))
    for project in sorted(vitest):
        suites.append(("vitest", project, tuple(sorted(vitest[project]))))
    for path in sorted(node):
        suites.append(("node", path, ()))
    for root, rel in sorted(pytest):
        suites.append(("pytest", rel if root == "." else f"{root}/{rel}", (root, rel)))
    return suites


def _needs_runtime(suites: list[tuple[str, str, tuple[str, ...]]]) -> bool:
    return any(kind in ("node", "pytest") for kind, _, _ in suites)


def _test_commands(suites: list[tuple[str, str, tuple[str, ...]]]) -> str:
    lines = ["rc=0"]
    if _needs_runtime(suites):
        lines.append("build_jan || exit 1")
    if any(kind == "vitest" for kind, _, _ in suites):
        lines.append("refresh_js || exit 1")
    for kind, location, extra in suites:
        lines.append(f'echo "{_SUITE_BEGIN} {kind} {location}"')
        if kind == "cargo":
            flags, libtest = _CRATE_FLAGS.get(location, ("", ""))
            lines.append(f"run_cargo {location} {shlex.quote(flags)} {shlex.quote(libtest)} || rc=$?")
        elif kind == "vitest":
            files = " ".join(shlex.quote(item) for item in extra)
            lines.append(f"run_vitest {location} {files} || rc=$?")
        elif kind == "node":
            lines.append(f"run_node {shlex.quote(location)} || rc=$?")
        else:
            lines.append(f"run_pytest {shlex.quote(extra[0])} {shlex.quote(extra[1])} || rc=$?")
        lines.append(f'echo "{_SUITE_END}"')
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _crates_to_fetch(suites: list[tuple[str, str, tuple[str, ...]]]) -> list[str]:
    crates = {location for kind, location, _ in suites if kind == "cargo"}
    if _needs_runtime(suites):
        crates.add("src-tauri/jan-cli")
    return sorted(crates)


def _provision(suites: list[tuple[str, str, tuple[str, ...]]]) -> str:
    lines = [f"fetch_crate {crate}" for crate in _crates_to_fetch(suites)]
    if any(kind == "vitest" for kind, _, _ in suites):
        lines.append("provision_js")
    return "\n".join(lines)


def _gate(suites: list[tuple[str, str, tuple[str, ...]]], sha: str) -> str:
    lines = [
        f'test "$(git rev-parse HEAD)" = "{sha}"',
        "cargo --version",
        "node --version",
        "yarn --version",
        'python -c "import pytest"',
    ]
    for crate in _crates_to_fetch(suites):
        lines.append(f"gate_crate {crate}")
    projects = [location for kind, location, _ in suites if kind == "vitest"]
    if projects:
        lines.append("test -f core/dist/index.js")
    for project in projects:
        lines.append(f"(cd {project} && yarn vitest --version)")
    return "\n".join(lines)


def _junit_cases(xml_text: str) -> list[tuple[list[str], str, str, str]]:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    cases: list[tuple[list[str], str, str, str]] = []

    def clean(value: Optional[str]) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    def walk(node: ET.Element, trail: list[str]) -> None:
        for child in node:
            if child.tag == "testsuite":
                walk(child, trail + [clean(child.get("name"))])
            elif child.tag == "testcase":
                tags = {grand.tag for grand in child}
                if tags & {"failure", "error"}:
                    status = "failed"
                elif "skipped" in tags:
                    status = "skipped"
                else:
                    status = "passed"
                cases.append(([t for t in trail if t], clean(child.get("classname")), clean(child.get("name")), status))

    holder = root
    if root.tag == "testsuite":
        holder = ET.Element("testsuites")
        holder.append(root)
    walk(holder, [])
    return cases


def _case_name(kind: str, location: str, trail: list[str], classname: str, name: str) -> str:
    if kind == "vitest":
        rel = trail[-1] if trail else classname
        return f"{location}/{rel} > {name}"
    if kind == "node":
        base = location.rsplit("/", 1)[-1]
        parts = [location] + [t for t in trail if not t.endswith(base)] + [name]
        return " > ".join(parts)
    module = location[:-3].split("/") if location.endswith(".py") else location.split("/")
    parts = classname.split(".") if classname else []
    keep = 0
    for size in range(min(len(parts), len(module)), 0, -1):
        if parts[:size] == module[-size:]:
            keep = size
            break
    rest = parts[keep:]
    return "::".join([location] + rest + [name])


class Jan9117To9388ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_RUST_IMAGE}

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
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_ENABLE_TELEMETRY=0 \\
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl file unzip xz-utils build-essential pkg-config cmake \\
    python3 python3-venv libssl-dev libdbus-1-dev libxdo-dev bubblewrap \\
    libglib2.0-dev libatk1.0-dev libpango1.0-dev libgtk-3-dev libsoup-3.0-dev \\
    libwebkit2gtk-4.1-dev librsvg2-dev libayatana-appindicator3-dev \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    ln -sf ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack && \\
    corepack enable && \\
    corepack prepare yarn@{_YARN_VERSION} --activate && \\
    node --version && npm --version && yarn --version

RUN python3 -m venv /opt/jan-python && \\
    /opt/jan-python/bin/pip install --no-cache-dir pytest=={_PYTEST_VERSION} && \\
    /opt/jan-python/bin/python -c "import pytest"

ENV PATH=/opt/jan-python/bin:${{PATH}}

RUN rustup component add rustfmt clippy && \\
    rustup target add aarch64-apple-darwin x86_64-apple-darwin && \\
    rustc --version && cargo --version

RUN git config --global --add safe.directory '*' && \\
    git config --global user.name "Jan CI" && \\
    git config --global user.email "ci@jan.ai"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Jan9117To9388ImageDefault(Image):
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
        return Jan9117To9388ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        suites = _suites(self.pr)
        test_commands = _test_commands(suites)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "{sha}^{{commit}}"; then
  git fetch --no-tags origin {sha}
fi
git checkout --detach {sha}
bash /home/check_git_changes.sh

{_ENV}

{_PREPARE_HELPERS}

{_provision(suites)}

{_gate(suites, sha)}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{_RUN_HELPERS}

cd /home/{repo}
{test_commands}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_commands}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{_RUN_HELPERS}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_commands}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {base.image_full_name()}

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
    rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test ! -e .git/ORIG_HEAD; \\
    test ! -e .git/FETCH_HEAD; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("janhq", _INTERVAL)
class JAN_9117_TO_9388(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Jan9117To9388ImageDefault(self.pr, self._config)

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

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        begin = re.compile(rf"^{re.escape(_SUITE_BEGIN)} (\S+) (\S+)\s*$")
        running = re.compile(r"^Running (unittests )?(\S+) \(")
        rust_result = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")

        def record(test: str, status: str) -> None:
            if status == "passed":
                passed_tests.add(test)
            elif status == "failed":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

        kind = ""
        location = ""
        target = ""
        junit: Optional[list[str]] = None
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            if junit is not None:
                if line.strip() == _JUNIT_END:
                    for trail, classname, name, status in _junit_cases("\n".join(junit)):
                        record(_case_name(kind, location, trail, classname, name), status)
                    junit = None
                else:
                    junit.append(line)
                continue
            stripped = line.strip()
            found = begin.match(stripped)
            if found:
                kind, location = found.group(1), found.group(2)
                target = ""
                continue
            if stripped == _SUITE_END:
                kind = location = target = ""
                continue
            if stripped == _JUNIT_BEGIN and kind in ("vitest", "node", "pytest"):
                junit = []
                continue
            if kind != "cargo":
                continue
            found = running.match(stripped)
            if found:
                target = f"{location}::{found.group(2)}" if found.group(1) else f"{location}/{found.group(2)}"
                continue
            found = rust_result.match(line.rstrip())
            if not found or not target:
                continue
            status = found.group(2)
            record(
                f"{target}::{found.group(1)}",
                "passed" if status == "ok" else "failed" if status == "FAILED" else "skipped",
            )

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
