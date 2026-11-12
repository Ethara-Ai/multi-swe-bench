import re
import shlex
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_INTERVAL = "rtk_776_to_4645"
_RUST_IMAGE = "rust:1.98.0-trixie"
_NODE_IMAGE = "node:22.23.4-trixie"
_SUITE_BEGIN = "rtk-suite-begin"
_SUITE_END = "rtk-suite-end"
_JUNIT_BEGIN = "rtk-junit-begin"
_JUNIT_END = "rtk-junit-end"
_SUITE_TIMEOUT = "3600"
_FILE_TIMEOUT = "900"

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
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C linker-features=-lld --cap-lints warn"
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS="--cap-lints warn"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1"""

_RUN_HELPERS = """JUNIT=/tmp/rtk-junit.xml

workspace_targets() {
  cargo metadata --format-version 1 --no-deps --locked | python3 -c 'import json,os,sys
root=os.getcwd()
for p in json.load(sys.stdin)["packages"]:
    where=os.path.relpath(p["manifest_path"],root)
    for t in p["targets"]:
        for k in t["kind"]:
            if k in ("lib","rlib","dylib","cdylib","staticlib","proc-macro"):
                print(p["name"],where,"lib",t["name"])
            elif k in ("bin","test"):
                print(p["name"],where,k,t["name"])'
}

run_cargo() {
  local status=0 targets pkg manifest kind name
  targets=$(workspace_targets | sort -u) || return 1
  while read -r pkg manifest kind name; do
    if [ -z "$pkg" ]; then
      continue
    fi
    echo "@BEGIN@ cargo $pkg/$kind/$name"
    case "$kind" in
      lib) timeout -k 60 @SUITE@ cargo test --locked --no-fail-fast --manifest-path "$manifest" --lib -- --test-threads=1 </dev/null || status=$? ;;
      bin) timeout -k 60 @SUITE@ cargo test --locked --no-fail-fast --manifest-path "$manifest" --bin "$name" -- --test-threads=1 </dev/null || status=$? ;;
      test) timeout -k 60 @SUITE@ cargo test --locked --no-fail-fast --manifest-path "$manifest" --test "$name" -- --test-threads=1 </dev/null || status=$? ;;
    esac
    echo "@END@"
  done <<< "$targets"
  return "$status"
}

run_python() {
  local status=0 dir
  for dir in $(find hooks -type d -name tests 2>/dev/null | sort); do
    if [ -z "$(find "$dir" -maxdepth 1 -type f -name 'test_*.py')" ]; then
      continue
    fi
    echo "@BEGIN@ python $dir"
    (cd "$(dirname "$dir")" && timeout -k 30 @FILE@ python3 -m unittest discover -s "$(basename "$dir")" -t . -v 2>&1) || status=$?
    echo "@END@"
  done
  return "$status"
}

run_node() {
  local status=0 file
  for file in $(find hooks -type f -name '*.test.mjs' 2>/dev/null | sort); do
    echo "@BEGIN@ node $file"
    rm -f "$JUNIT"
    timeout -k 30 @FILE@ node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination="$JUNIT" "$file" || status=$?
    echo "@JUNIT_BEGIN@"
    if [ -f "$JUNIT" ]; then
      cat "$JUNIT"
      echo
    fi
    echo "@JUNIT_END@"
    echo "@END@"
  done
  return "$status"
}

run_shell() {
  local status=0 file hook
  cargo build --locked --bins </dev/null || status=$?
  for file in $(find hooks -type f -name 'test-*.sh' 2>/dev/null | sort); do
    hook="$(dirname "$file")/$(basename "$file" | sed 's/^test-//')"
    echo "@BEGIN@ shell $file"
    HOOK="$PWD/$hook" PATH="$PWD/target/debug:$PATH" timeout -k 30 @FILE@ bash "$file" </dev/null || status=$?
    echo "@END@"
  done
  return "$status"
}

run_all() {
  local rc=0
  run_cargo || rc=$?
  run_python || rc=$?
  run_node || rc=$?
  run_shell || rc=$?
  return "$rc"
}"""

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
}"""


def _render(text: str) -> str:
    for key, value in (
        ("@BEGIN@", _SUITE_BEGIN),
        ("@END@", _SUITE_END),
        ("@JUNIT_BEGIN@", _JUNIT_BEGIN),
        ("@JUNIT_END@", _JUNIT_END),
        ("@SUITE@", _SUITE_TIMEOUT),
        ("@FILE@", _FILE_TIMEOUT),
    ):
        text = text.replace(key, value)
    return text


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if head and re.search(r"^Binary files .* differ$", block, re.M) and "GIT binary patch" not in block:
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [f"--exclude={shlex.quote(path)}" for path in sorted(skipped)]
    return " ".join(flags)


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


class Rtk776To4645ImageBase(Image):
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
    git ca-certificates curl build-essential pkg-config procps \\
    python3 jq ripgrep \\
    && rm -rf /var/lib/apt/lists/*

COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node
COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \\
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \\
    node --version && npm --version && rustc --version && cargo --version && \\
    python3 --version && jq --version && rg --version

RUN git config --global --add safe.directory '*' && \\
    git config --global user.name "rtk CI" && \\
    git config --global user.email "ci@rtk.invalid"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Rtk776To4645ImageDefault(Image):
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
        return Rtk776To4645ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
        helpers = _render(_RUN_HELPERS)
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

fetch_workspace

test "$(git rev-parse HEAD)" = "{sha}"
cargo --version
rustc --version
node --version
python3 --version
jq --version
rg --version
cargo metadata --locked --offline --filter-platform "$(host_triple)" --format-version 1 >/dev/null
cargo check --locked --offline --workspace --all-targets
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{helpers}

cd /home/{repo}
run_all
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{helpers}

cd /home/{repo}
git apply {apply} /home/test.patch
run_all
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

{helpers}

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
run_all
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


@Instance.register("rtk-ai", "rtk")
@Instance.register("rtk-ai", _INTERVAL)
class RTK_776_TO_4645(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Rtk776To4645ImageDefault(self.pr, self._config)

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
        begin = re.compile(rf"^{re.escape(_SUITE_BEGIN)} (cargo|python|node|shell) (\S+)$")
        cargo_case = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")
        python_case = re.compile(r"^(\w+) \(([\w.]+)\)(?: \.\.\. (.+))?$")
        python_tail = re.compile(r" \.\.\. (.+)$")
        shell_section = re.compile(r"^-{3} (.+?) -{3}$")
        shell_case = re.compile(r"^(PASS|FAIL|DENY|REWRITE) (.+?)(?: → .*)?$")

        def record(test: str, status: str) -> None:
            if status == "passed":
                passed_tests.add(test)
            elif status == "failed":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

        def python_status(word: str) -> str:
            word = word.strip()
            if word in ("ok", "expected failure"):
                return "passed"
            if word.startswith("skipped"):
                return "skipped"
            return "failed"

        kind = ""
        location = ""
        section = ""
        pending = ""
        junit: Optional[list[str]] = None
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            stripped = line.strip()
            if junit is not None:
                if stripped == _JUNIT_END:
                    for trail, name, status in _junit_cases("\n".join(junit)):
                        record(" > ".join([location] + trail + [name]), status)
                    junit = None
                else:
                    junit.append(line)
                continue
            found = begin.match(stripped)
            if found:
                kind, location = found.group(1), found.group(2)
                section = pending = ""
                continue
            if stripped == _SUITE_END:
                kind = location = section = pending = ""
                continue
            if kind == "cargo":
                found = cargo_case.match(line.rstrip())
                if found:
                    status = found.group(2)
                    record(
                        f"{location}::{found.group(1)}",
                        "passed" if status == "ok" else "failed" if status == "FAILED" else "skipped",
                    )
            elif kind == "python":
                found = python_case.match(stripped)
                if found:
                    if found.group(3):
                        record(f"{location}::{found.group(2)}", python_status(found.group(3)))
                        pending = ""
                    else:
                        pending = found.group(2)
                    continue
                found = python_tail.search(stripped)
                if found and pending:
                    record(f"{location}::{pending}", python_status(found.group(1)))
                    pending = ""
            elif kind == "node":
                if stripped == _JUNIT_BEGIN:
                    junit = []
            elif kind == "shell":
                found = shell_section.match(stripped)
                if found:
                    section = found.group(1)
                    continue
                found = shell_case.match(stripped)
                if found:
                    name = "::".join(part for part in (location, section, found.group(2)) if part)
                    record(name, "failed" if found.group(1) == "FAIL" else "passed")

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
