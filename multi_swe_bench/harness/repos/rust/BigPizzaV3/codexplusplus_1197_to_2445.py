import re
import shlex
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL = "codexplusplus_1197_to_2445"
_RUST_IMAGE = "rust:1.98.0-trixie"
_NODE_IMAGE = "node:22.23.4-trixie"
_SUITE_BEGIN = "cpp-suite-begin"
_SUITE_END = "cpp-suite-end"
_JUNIT_BEGIN = "cpp-junit-begin"
_JUNIT_END = "cpp-junit-end"
_SUITE_TIMEOUT = 3600
_FILE_TIMEOUT = 900

_RUST_CRATE = re.compile(r"^(.+?)/(?:src|tests|benches|examples)/.+\.rs$|^(.+?)/build\.rs$")
_NODE_TEST = re.compile(r"^(.+?)/((?:src/)?)[^/]+\.test\.(ts|mts|mjs|js)$")

_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
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
export npm_config_fund=false
export npm_config_audit=false
export npm_config_update_notifier=false"""

_RUN_HELPERS = f"""JUNIT=/tmp/cpp-junit.xml

emit_junit() {{
  echo "{_JUNIT_BEGIN}"
  if [ -f "$JUNIT" ]; then
    cat "$JUNIT"
    echo
  fi
  echo "{_JUNIT_END}"
}}

crate_info() {{
  (cd "$1" && cargo metadata --format-version 1 --no-deps --offline) | python3 -c 'import json,os,sys
want=os.path.realpath(os.path.join(sys.argv[1],"Cargo.toml"))
pkg=next(p for p in json.load(sys.stdin)["packages"] if os.path.realpath(p["manifest_path"])==want)
kinds={{k for t in pkg["targets"] for k in t["kind"]}}
print("lib" if kinds & {{"lib","rlib","cdylib","staticlib","dylib","proc-macro"}} else "")
print("bin" if "bin" in kinds else "")
for t in pkg["targets"]:
    if "test" in t["kind"]:
        print("test:"+t["name"])' "$1"
}}

run_cargo() {{
  local crate="$1" status=0 entry info
  if [ ! -f "$crate/Cargo.toml" ]; then
    echo "cargo crate $crate not present at this commit"
    return 0
  fi
  info=$(crate_info "$crate") || return 1
  for entry in $info; do
    case "$entry" in
      lib) (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test --locked --no-fail-fast --lib -- --test-threads=1) || status=$? ;;
      bin) (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test --locked --no-fail-fast --bins -- --test-threads=1) || status=$? ;;
      test:*) (cd "$crate" && timeout -k 60 {_SUITE_TIMEOUT} cargo test --locked --no-fail-fast --test "${{entry#test:}}" -- --test-threads=1) || status=$? ;;
    esac
  done
  return "$status"
}}

run_node() {{
  local project="$1" dir="$2" ext="$3" status=0 file
  if [ ! -d "$project/$dir" ]; then
    echo "node project $project/$dir not present at this commit"
    return 0
  fi
  for file in $(cd "$project" && find "${{dir:-.}}" -maxdepth 1 -type f -name "*.test.$ext" | sed 's|^\\./||' | sort); do
    echo "{_SUITE_BEGIN} node $project/$file"
    rm -f "$JUNIT"
    (cd "$project" && timeout -k 30 {_FILE_TIMEOUT} node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="$JUNIT" "$file") || status=$?
    emit_junit
    echo "{_SUITE_END}"
  done
  return "$status"
}}"""

_PREPARE_HELPERS = """host_triple() {
  rustc -vV | sed -n 's/^host: //p'
}

fetch_workspace() {
  local attempt host
  host=$(host_triple)
  for attempt in 1 2 3 4 5; do
    if cargo fetch --locked --target "$host"; then
      return 0
    fi
    sleep 30
  done
  return 1
}

install_node() {
  local project="$1" attempt
  if [ -d "$project/node_modules" ]; then
    return 0
  fi
  for attempt in 1 2 3 4 5; do
    if [ -f "$project/package-lock.json" ]; then
      if (cd "$project" && npm ci --no-audit --no-fund); then
        return 0
      fi
    elif (cd "$project" && npm install --no-audit --no-fund); then
      return 0
    fi
    rm -rf "$project/node_modules"
    sleep 30
  done
  return 1
}

gate_node() {
  (cd "$1" && node -e 'const fs=require("fs");const p=require("./package.json");for (const d of Object.keys({...p.dependencies,...p.devDependencies})) fs.accessSync("node_modules/"+d+"/package.json")')
}

tauri_value() {
  python3 -c 'import json,os,sys
conf=json.load(open(os.path.join(sys.argv[1],"tauri.conf.json")))
build=conf.get("build",{})
if sys.argv[2]=="dist":
    print(os.path.normpath(os.path.join(sys.argv[1],build.get("frontendDist") or build.get("distDir") or "../dist")))
else:
    print(build.get("beforeBuildCommand") or "")' "$1" "$2"
}

tauri_frontend() {
  local crate="$1" dist cmd app
  if [ ! -f "$crate/tauri.conf.json" ]; then
    return 0
  fi
  dist=$(tauri_value "$crate" dist)
  cmd=$(tauri_value "$crate" command)
  app=$(dirname "$crate")
  if [ -n "$cmd" ] && [ -f "$app/package.json" ]; then
    install_node "$app"
    (cd "$app" && bash -c "$cmd")
  fi
  mkdir -p "$dist"
}

gate_tauri() {
  local crate="$1"
  if [ -f "$crate/tauri.conf.json" ]; then
    test -d "$(tauri_value "$crate" dist)"
  fi
}"""


def _paths(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch, re.M)


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if head and re.search(r"^Binary files .* differ$", block, re.M) and "GIT binary patch" not in block:
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [f"--exclude={shlex.quote(path)}" for path in sorted(skipped)]
    return " ".join(flags)


def _suites(pr: PullRequest) -> list[tuple[str, str, tuple[str, ...]]]:
    crates: set[str] = set()
    node: set[tuple[str, str, str]] = set()
    for path in _paths(pr.test_patch) + _paths(pr.fix_patch):
        found = _RUST_CRATE.match(path)
        if found:
            crates.add(found.group(1) or found.group(2))
    for path in _paths(pr.test_patch):
        found = _NODE_TEST.match(path)
        if found:
            node.add((found.group(1), found.group(2).rstrip("/"), found.group(3)))
    suites: list[tuple[str, str, tuple[str, ...]]] = []
    for crate in sorted(crates):
        suites.append(("cargo", crate, ()))
    for project, directory, ext in sorted(node):
        suites.append(("node", project, (directory, ext)))
    return suites


def _test_commands(suites: list[tuple[str, str, tuple[str, ...]]]) -> str:
    lines = ["rc=0"]
    for kind, location, extra in suites:
        if kind == "cargo":
            lines.append(f'echo "{_SUITE_BEGIN} cargo {location}"')
            lines.append(f"run_cargo {shlex.quote(location)} || rc=$?")
            lines.append(f'echo "{_SUITE_END}"')
        else:
            lines.append(f"run_node {shlex.quote(location)} {shlex.quote(extra[0])} {shlex.quote(extra[1])} || rc=$?")
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _provision(suites: list[tuple[str, str, tuple[str, ...]]]) -> str:
    lines = ["fetch_workspace"]
    for kind, location, _ in suites:
        if kind == "cargo":
            lines.append(f"tauri_frontend {shlex.quote(location)}")
        else:
            lines.append(f"install_node {shlex.quote(location)}")
    return "\n".join(lines)


def _gate(suites: list[tuple[str, str, tuple[str, ...]]], sha: str) -> str:
    lines = [
        f'test "$(git rev-parse HEAD)" = "{sha}"',
        "cargo --version",
        "node --version",
        "npm --version",
        'cargo metadata --locked --offline --filter-platform "$(host_triple)" --format-version 1 >/dev/null',
    ]
    for kind, location, _ in suites:
        if kind == "cargo":
            lines.append(f"gate_tauri {shlex.quote(location)}")
        else:
            lines.append(f"gate_node {shlex.quote(location)}")
    return "\n".join(lines)


def _junit_cases(xml_text: str) -> list[tuple[list[str], str, str]]:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []
    cases: list[tuple[list[str], str, str]] = []

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
                cases.append(([t for t in trail if t], clean(child.get("name")), status))

    holder = root
    if root.tag == "testsuite":
        holder = ET.Element("testsuites")
        holder.append(root)
    walk(holder, [])
    return cases


class CodexPlusPlus1197To2445ImageBase(Image):
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
    python3 libssl-dev libdbus-1-dev libxdo-dev \\
    libglib2.0-dev libatk1.0-dev libpango1.0-dev libgtk-3-dev libsoup-3.0-dev \\
    libwebkit2gtk-4.1-dev librsvg2-dev libayatana-appindicator3-dev \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version && rustc --version && cargo --version

RUN git config --global --add safe.directory '*' && \\
    git config --global user.name "CodexPlusPlus CI" && \\
    git config --global user.email "ci@codexplusplus.invalid"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class CodexPlusPlus1197To2445ImageDefault(Image):
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
        return CodexPlusPlus1197To2445ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        suites = _suites(self.pr)
        test_commands = _test_commands(suites)
        apply = _apply_flags(self.pr)
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
git apply {apply} /home/test.patch
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
git apply {apply} /home/test.patch /home/fix.patch
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
    git gc --prune=now; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("BigPizzaV3", _INTERVAL)
class CODEXPLUSPLUS_1197_TO_2445(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CodexPlusPlus1197To2445ImageDefault(self.pr, self._config)

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
                    for trail, name, status in _junit_cases("\n".join(junit)):
                        record(" > ".join([location] + trail + [name]), status)
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
            if stripped == _JUNIT_BEGIN and kind == "node":
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
