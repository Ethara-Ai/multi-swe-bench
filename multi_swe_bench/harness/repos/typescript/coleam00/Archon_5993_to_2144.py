import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "Archon_5993_to_2144"
_NODE_IMAGE = "node:22-bookworm"
_BUN_VERSION = "1.4.2"
_TEST_TIMEOUT_MS = "20000"
_FILE_TIMEOUT_S = "1800"
_APT = "git ca-certificates curl unzip procps"
_CERT = "/etc/ssl/certs/ca-certificates.crt"
_PROXY = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
_NOPROXY = ("no_proxy", "NO_PROXY")
_CA_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_INFRA_ENV = "DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC".split()
_APP_ENV = "CI=true ARCHON_TELEMETRY_DISABLED=1 NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}".split()
_CERT_LINKS = (
    "/etc/pki/tls/certs/ca-bundle.crt /etc/ssl/cert.pem /etc/ssl/ca-bundle.pem"
    " /etc/pki/tls/cacert.pem /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"
    " /etc/ssl/certs/ca-bundle.crt").split()
_GIT_CONFIG = (
    "git config --global --add safe.directory '*'", "git config --global http.version HTTP/1.1",
    "git config --global init.defaultBranch main", 'git config --global user.name "mswebench"',
    'git config --global user.email "mswebench@example.com"')
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_TEST_PATH = re.compile(r"\.test\.[cm]?[jt]sx?$")
_SOURCE_PATH = re.compile(r"\.[cm]?[jt]sx?$")
_BUN_DIR = re.compile(r"^MSWB_DIR (\S+)$")
_BUN_FILE = re.compile(r"^(\S+\.test\.[cm]?[jt]sx?):$")
_BUN_RESULT = re.compile(r"^\((pass|fail|skip|todo)\) (.+?)(?: \[[0-9.]+ ?m?s\])?$")
_VOLATILE_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")
_VOLATILE_NUM = re.compile(r"\b\d{10,}\b")
_VOLATILE_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
_VOLATILE_HOST = re.compile(r'("host"\s*:\s*")[0-9a-f]{12}(")')
_VOLATILE_PID = re.compile(r'("pid"\s*:\s*)\d+')
_VOLATILE_TMP = re.compile(r"/tmp/[A-Za-z0-9._-]*[0-9][A-Za-z0-9._-]*")

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = f"{_APPLY_TEST}\ngit apply --whitespace=nowarn /home/fix.patch"

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

_SELECT = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
out=/home/test_list.txt
: > "$out"
while IFS= read -r f; do
    [ -n "$f" ] || continue
    [ -f "$f" ] || continue
    case "$f" in
        scripts/*|.archon/scripts/*) d="." ;;
        *)
            d=$(dirname "$f")
            while [ "$d" != "." ] && [ ! -f "$d/package.json" ]; do
                d=$(dirname "$d")
            done
            ;;
    esac
    printf '%s\t%s\n' "$d" "${f#"$d"/}" >> "$out"
done < /home/test_files.txt
sort -u -o "$out" "$out"
echo "MSWB_SELECTED $(wc -l < "$out")"
cat "$out"
"""

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, pinned by this commit's bun.lock) ----------
export CI=true
export ARCHON_TELEMETRY_DISABLED=1
bun --version
bun install --frozen-lockfile || bun install

# ---------- Section 3: HARD GATE (non-tolerant) ----------
test -d /home/[[REPO]]/node_modules
bash /home/select_tests.sh
test -s /home/test_list.txt
IFS=$'\\t' read -r d rel < /home/test_list.txt
p=""
if [ "$d" = "." ] && [ -f ./.archon/scripts/__tests__/archon-script-preload.ts ]; then
    p="--preload ./.archon/scripts/__tests__/archon-script-preload.ts"
fi
(cd "$d" && bun test --pass-with-no-tests --timeout [[TIMEOUT_MS]] \\
    $p -t '^mswb_no_match$' "./$rel" < /dev/null)
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true
export ARCHON_TELEMETRY_DISABLED=1
export ARCHON_HOME=/tmp/archon-home
export BUN_RUNTIME_TRANSPILER_CACHE_PATH=/home/[[REPO]]/node_modules/.cache/bun-transpiler

cd /home/[[REPO]]
[[APPLY]]
if ! git diff --quiet HEAD -- '*package.json' '*bun.lock' 2>/dev/null; then
    irc=0
    bun install --frozen-lockfile > /dev/null 2>&1 \\
        || bun install > /dev/null 2>&1 \\
        || irc=$?
    if [ "$irc" -ne 0 ]; then
        echo "MSWB_INSTALL_RESYNC_FAILED exit $irc"
    fi
fi
bash /home/select_tests.sh

preload=""
if [ -f ./.archon/scripts/__tests__/archon-script-preload.ts ]; then
    preload="--preload ./.archon/scripts/__tests__/archon-script-preload.ts"
fi

log=/tmp/mswb-tests.log
: > "$log"
broken=0
while IFS=$'\\t' read -r dir rel; do
    [ -n "$rel" ] || continue
    args=""
    if [ "$dir" = "." ]; then
        args="$preload"
    fi
    echo "MSWB_DIR $dir"
    one=/tmp/mswb-one.log
    : > "$one"
    rc=0
    (cd "$dir" && timeout [[TIMEOUT_S]] bun test --timeout [[TIMEOUT_MS]] \\
        $args "./$rel" < /dev/null) > "$one" 2>&1 || rc=$?
    cat "$one"
    cat "$one" >> "$log"
    cnt=$(grep -cE '^\\((pass|fail|skip|todo)\\) ' "$one" || true)
    if [ "$cnt" -eq 0 ]; then
        echo "MSWB_SUITE_ERROR $dir/$rel exit $rc produced no results"
        broken=$((broken + 1))
    fi
done < /home/test_list.txt

n=$(grep -cE '^\\((pass|fail|skip|todo)\\) ' "$log" || true)
echo "MSWB_RESULT_COUNT $n"
if [ "$broken" -gt 0 ]; then
    echo "MSWB_BROKEN_SUITES $broken"
fi
if [ "$n" -eq 0 ]; then
    echo "MSWB_ZERO_RESULTS"
    exit 1
fi
"""

def _patch_files(patch: str) -> dict[str, bool]:
    found: dict[str, bool] = {}
    current: Optional[str] = None
    for line in (patch or "").splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            current = header.group(2)
            found[current] = False
        elif current and line.startswith("deleted file mode"):
            found[current] = True
    return found

def _test_files(pr: PullRequest) -> str:
    found: dict[str, None] = {}
    for patch in (pr.test_patch, pr.fix_patch):
        for path, deleted in _patch_files(patch).items():
            if deleted or not _SOURCE_PATH.search(path):
                continue
            if _TEST_PATH.search(path):
                found[path] = None
                continue
            stem = path.rpartition(".")[0]
            directory, _, base = stem.rpartition("/")
            for candidate in (f"{stem}.test.ts", f"{stem}.test.tsx",
                              f"{directory}/__tests__/{base}.test.ts" if directory else ""):
                if candidate:
                    found[candidate] = None
    return "".join(f"{path}\n" for path in found)

def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    for token, value in (("[[APPLY]]", apply), ("[[REPO]]", pr.repo),
                         ("[[TIMEOUT_MS]]", _TEST_TIMEOUT_MS), ("[[TIMEOUT_S]]", _FILE_TIMEOUT_S)):
        template = template.replace(token, value)
    return template

class _Layer(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
    @property
    def pr(self) -> PullRequest:
        return self._pr
    @property
    def config(self) -> Config:
        return self._config
    def workdir(self) -> str:
        return self.image_tag()

class ArchonImageBase(_Layer):
    def dependency(self) -> str:
        return _NODE_IMAGE
    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"
    def files(self) -> list[File]:
        return []
    def dockerfile(self) -> str:
        org, repo = self.pr.org, self.pr.repo
        args = [f'ARG {name}=""' for name in _PROXY]
        args += [f'ARG {name}="localhost,127.0.0.1,::1"' for name in _NOPROXY]
        env = list(_INFRA_ENV)
        env += [f"{name}=${{{name}}}" for name in _PROXY + _NOPROXY]
        env += [f"{name}=${{CA_CERT_PATH}}" for name in _CA_ENV]
        links = [f"ln -sf {_CERT} {target}" for target in _CERT_LINKS]
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{chr(10).join(args)}
ARG CA_CERT_PATH="{_CERT}"

ENV {" \\\n    ".join(env)}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    {" && \\\n    ".join(links)}

ENV {" \\\n    ".join(_APP_ENV)}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_APT} \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) bun_pkg=bun-linux-x64-baseline ;; \\
        arm64) bun_pkg=bun-linux-aarch64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v{_BUN_VERSION}/${{bun_pkg}}.zip"; \\
    unzip -q /tmp/bun.zip -d /tmp; \\
    install -m 0755 "/tmp/${{bun_pkg}}/bun" /usr/local/bin/bun; \\
    rm -rf /tmp/bun.zip "/tmp/${{bun_pkg}}"; \\
    bun --version

RUN {" && \\\n    ".join(_GIT_CONFIG)}

RUN git --version && node --version && bun --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""

class ArchonImageDefault(_Layer):
    def dependency(self) -> Image:
        return ArchonImageBase(self.pr, self._config)
    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"
    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "test_files.txt", _test_files(pr)),
            File(".", "select_tests.sh", _render(_SELECT, pr)),
            File(".", "prepare.sh", _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
        ]
    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {self.dependency().image_full_name()}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}"""

@Instance.register("coleam00", _INTERVAL)
@Instance.register("coleam00", "Archon")
class ARCHON_5993_TO_2144(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
    @property
    def pr(self) -> PullRequest:
        return self._pr
    def dependency(self) -> Optional[Image]:
        return ArchonImageDefault(self.pr, self._config)
    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"
    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"
    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"
    def parse_log(self, test_log: str) -> TestResult:
        status: dict[str, str] = {}
        prefix = ""
        current: Optional[str] = None
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            directory = _BUN_DIR.match(line)
            if directory:
                prefix = "" if directory.group(1) == "." else f"{directory.group(1)}/"
                current = None
                continue
            header = _BUN_FILE.match(line)
            if header:
                current = prefix + header.group(1).removeprefix("./")
                continue
            result = _BUN_RESULT.match(line)
            if not result or not current:
                continue
            name = _VOLATILE_ADDR.sub("0x<addr>", result.group(2).strip())
            name = _VOLATILE_UUID.sub("<uuid>", name)
            name = _VOLATILE_HOST.sub(r"\1<host>\2", name)
            name = _VOLATILE_PID.sub(r"\1<pid>", name)
            name = _VOLATILE_TMP.sub("/tmp/<tmp>", name)
            key = f"{current}::{_VOLATILE_NUM.sub('<num>', name)}"
            state = result.group(1)
            status[key] = "fail" if status.get(key) == "fail" else state
        pick = lambda *wanted: {k for k, v in status.items() if v in wanted}
        ok, bad, skip = pick("pass"), pick("fail"), pick("skip", "todo")
        return TestResult(len(ok), len(bad), len(skip), ok, bad, skip)
