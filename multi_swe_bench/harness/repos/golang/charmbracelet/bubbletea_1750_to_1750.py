import json
import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "bubbletea_1750_to_1750"
_GO_IMAGE = "golang:1.25.14-bookworm"
_EXTRA_APT = ""
_CGO_ENABLED = "0"
_GO_TEST_FLAGS = ""
_BUN_VERSION = ""
_JS_TEST_ROOT = ""

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_HEADER = re.compile(r"^@@ [^@]* @@ ?(.*)$")
_TEST_FUNC = re.compile(r"^func ((?:Test|Fuzz|Example)\w*)\(")
_VOLATILE_TIME = re.compile(r"time\.Date\([^()]*\)")
_VOLATILE_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")
_BUN_FILE = re.compile(r"^(\S+\.test\.[cm]?[jt]sx?):$")
_BUN_RESULT = re.compile(r"^\((pass|fail|skip|todo)\) (.+?)(?: \[[0-9.]+m?s\])?$")

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)

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

_EXTRA_PROVISION = """"""

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, pinned by this commit's go.sum) ----------
go mod download
[[EXTRA_PROVISION]]
[[JS_PROVISION]]
# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Builds every package and compiles the test binaries of every package this PR
# touches, which also warms the build cache the three graded runs reuse.
go build ./...
bash /home/select_pkgs.sh
if [ -s /home/test_pkgs.txt ]; then
    xargs go test -count=1 [[GO_TEST_FLAGS]] -run '^$' < /home/test_pkgs.txt > /dev/null
fi
[[JS_GATE]]
echo DEPS_OK
"""

_JS_PROVISION = """JS_VERSION=$(awk -F'"' '/"version"/ {print $4}' [[JSROOT]]/package.json)
GOOS=js GOARCH=wasm go build -ldflags="-s -w -X github.com/[[ORG]]/[[REPO]]/d2js/d2wasm.jsVersion=${JS_VERSION}" -trimpath -o [[JSROOT]]/wasm/d2.wasm ./d2js
"""

_JS_GATE = """(cd [[JSROOT]] && bun build.js > /dev/null)
git checkout -- [[JSROOT]]/src/platform.js [[JSROOT]]/src/worker.js
bun --version
test -s [[JSROOT]]/wasm/d2.wasm
"""

_SELECT = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
out=/home/test_pkgs.txt
: > "$out"
while IFS= read -r f; do
    [ -n "$f" ] || continue
    d=$(dirname "$f")
    while :; do
        case "/$d/" in
            */testdata/*|*/vendor/*|*/node_modules/*) ;;
            *)
                if [ -d "$d" ] && ls "$d"/*.go > /dev/null 2>&1 && go list "./$d" > /dev/null 2>&1; then
                    echo "./$d" >> "$out"
                    break
                fi
                ;;
        esac
        [ "$d" = "." ] && break
        d=$(dirname "$d")
    done
done < /home/test_files.txt
sort -u -o "$out" "$out"
echo "selected packages:"
cat "$out"
"""

_GUARD = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
targets=/home/test_targets.txt
[ -s "$targets" ] || exit 0

cut -f1 "$targets" | sort -u | while read -r dir; do
    [ -d "$dir" ] || continue
    if go test -count=1 [[GO_TEST_FLAGS]] -run '^$' "./$dir" > /dev/null 2>&1; then
        continue
    fi
    pkg=$(go list -e -f '{{.ImportPath}}' "./$dir")
    awk -F'\\t' -v d="$dir" '$1 == d {print $2 "\\t" $3}' "$targets" | sort -u | while IFS=$'\\t' read -r file name; do
        if [ -n "$name" ] && [ -f "$file" ] && grep -q "^func ${name}(" "$file"; then
            printf '{"Action":"fail","Package":"%s","Test":"%s"}\\n' "$pkg" "$name"
        fi
    done
    git status --porcelain --untracked-files=all -- "$dir" | cut -c4- | while read -r file; do
        case "$file" in
            *_test.go) ;;
            *) continue ;;
        esac
        [ "$(dirname "$file")" = "$dir" ] || continue
        if git cat-file -e "HEAD:$file" 2>/dev/null; then
            git checkout -q HEAD -- "$file"
        else
            rm -f "$file"
        fi
    done
done
"""

_RUN_GO = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
bash /home/guard_tests.sh
bash /home/select_pkgs.sh
if [ -s /home/test_pkgs.txt ]; then
    xargs go test -json -count=1 -timeout 30m [[GO_TEST_FLAGS]] < /home/test_pkgs.txt
fi
"""

_RUN_JS = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
JS_VERSION=$(awk -F'"' '/"version"/ {print $4}' [[JSROOT]]/package.json)
GOOS=js GOARCH=wasm go build -ldflags="-s -w -X github.com/[[ORG]]/[[REPO]]/d2js/d2wasm.jsVersion=${JS_VERSION}" -trimpath -o [[JSROOT]]/wasm/d2.wasm ./d2js
cd [[JSROOT]]
bun build.js
bun test test/unit test/integration 2>&1
"""


def _test_name(text: str) -> Optional[str]:
    match = _TEST_FUNC.match(text)
    if match and match.group(1) != "TestMain":
        return match.group(1)
    return None


def _patch_files(patch: str) -> list[str]:
    found: dict[str, None] = {}
    for line in patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            found[header.group(2)] = None
    return list(found)


def _changed_tests(test_patch: str) -> list[tuple[str, str]]:
    found: dict[tuple[str, str], None] = {}
    path: Optional[str] = None
    in_hunk = False
    current: Optional[str] = None
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith("_test.go") else None
            in_hunk = False
            current = None
            continue
        if path is None:
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            in_hunk = True
            current = _test_name(hunk.group(1))
            continue
        if not in_hunk or not line or line[0] not in " +-":
            continue
        body = line[1:]
        if body.startswith("func "):
            current = _test_name(body)
        if line[0] != " " and current:
            found[(path, current)] = None
    return list(found)


def _test_targets(test_patch: str) -> str:
    rows: dict[str, None] = {}
    for path, name in _changed_tests(test_patch):
        directory = posixpath.dirname(path) or "."
        rows[f"{directory}\t{path}\t{name}"] = None
    for path in _patch_files(test_patch):
        if path.endswith("_test.go"):
            rows[f"{posixpath.dirname(path) or '.'}\t{path}\t"] = None
    return "".join(f"{row}\n" for row in rows)


def _is_js_pr(pr: PullRequest) -> bool:
    if not _JS_TEST_ROOT:
        return False
    tests = _patch_files(pr.test_patch)
    return bool(tests) and all(t.startswith(_JS_TEST_ROOT + "/") for t in tests)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[GO_TEST_FLAGS]]", _GO_TEST_FLAGS)
        .replace("[[JSROOT]]", _JS_TEST_ROOT)
        .replace("[[ORG]]", pr.org)
        .replace("[[REPO]]", pr.repo)
    )


class Bubbletea1750To1750ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def _bun_block(self) -> str:
        if not _BUN_VERSION:
            return ""
        return f"""RUN set -eux; \
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \
    case "$arch" in \
        amd64) bun_pkg=bun-linux-x64-baseline ;; \
        arm64) bun_pkg=bun-linux-aarch64 ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v{_BUN_VERSION}/${{bun_pkg}}.zip"; \
    unzip -q /tmp/bun.zip -d /tmp; \
    install -m 0755 "/tmp/${{bun_pkg}}/bun" /usr/local/bin/bun; \
    rm -rf /tmp/bun.zip "/tmp/${{bun_pkg}}"; \
    bun --version

"""

    def dockerfile(self) -> str:
        apt = " ".join(p for p in ["git ca-certificates build-essential", _EXTRA_APT] if p)
        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV CGO_ENABLED={_CGO_ENABLED} \
    GOTOOLCHAIN=local \
    GOFLAGS=-buildvcs=false \
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
    {apt} \
    && rm -rf /var/lib/apt/lists/*

{self._bun_block()}RUN git config --global --add safe.directory '*' && \
    git config --global http.version HTTP/1.1 && \
    git config --global user.name "mswebench" && \
    git config --global user.email "mswebench@example.com" && \
    go version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Bubbletea1750To1750ImageDefault(Image):
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
        return Bubbletea1750To1750ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        js = _is_js_pr(pr)
        prepare = (
            _render(_PREPARE, pr)
            .replace("[[SHA]]", pr.base.sha)
            .replace("[[EXTRA_PROVISION]]", _EXTRA_PROVISION)
            .replace("[[JS_PROVISION]]", _render(_JS_PROVISION, pr) if js else "")
            .replace("[[JS_GATE]]", _render(_JS_GATE, pr) if js else "")
        )
        run = _RUN_JS if js else _RUN_GO
        changed = _patch_files(pr.test_patch) + _patch_files(pr.fix_patch)
        test_files = "".join(f"{f}\n" for f in dict.fromkeys(changed))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test_files.txt", test_files),
            File(".", "test_targets.txt", _test_targets(pr.test_patch)),
            File(".", "select_pkgs.sh", _render(_SELECT, pr)),
            File(".", "guard_tests.sh", _render(_GUARD, pr)),
            File(".", "run.sh", _render(run, pr)),
            File(".", "test-run.sh", _render(run, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(run, pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/{repo}; \
    git checkout --detach "{sha}"; \
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
    test "$(git rev-parse HEAD)" = "{sha}"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \
        cd /home/{repo} && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""


@Instance.register("charmbracelet", _INTERVAL)
class BUBBLETEA_1750_TO_1750(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Bubbletea1750To1750ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        bun_file: Optional[str] = None
        for raw in clean.splitlines():
            line = raw.strip()
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                package = event.get("Package")
                test = event.get("Test")
                if not package or not test:
                    continue
                test = _VOLATILE_TIME.sub("time.Date(<time>)", test)
                test = _VOLATILE_ADDR.sub("0x<addr>", test)
                name = f"{package}::{test}"
                action = event.get("Action")
                if action == "pass":
                    passed_tests.add(name)
                elif action == "fail":
                    failed_tests.add(name)
                elif action == "skip":
                    skipped_tests.add(name)
                continue
            header = _BUN_FILE.match(line)
            if header:
                bun_file = header.group(1)
                continue
            result = _BUN_RESULT.match(line)
            if result and bun_file:
                name = f"{bun_file}::{result.group(2)}"
                status = result.group(1)
                if status == "pass":
                    passed_tests.add(name)
                elif status == "fail":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
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
