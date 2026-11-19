import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "postgres_language_server_888_to_888"

# Toolchain pin: rust-toolchain.toml at the base sha says channel = "1.99.0" with target
# wasm32-unknown-emscripten (pre-installed below so rustup never fetches it at run time).
# Official Docker library image via its ECR Public mirror (same digests as Docker Hub,
# multi-arch, not subject to Hub pull-rate limits).
_RUST_IMAGE = "public.ecr.aws/docker/library/rust:1.99.0-bookworm"
# libclang/clang: bindgen in crates/pgls_query/build.rs; nodejs: JS runtime for
# `tree-sitter generate` (crates/pgls_treesitter_grammar/build.rs); postgresql-15: the
# tests are #[sqlx::test] against a live server (the repo's Dockerfile and CI use PG 15,
# which is also Debian bookworm's packaged major); libicu-dev + server-dev for plpgsql_check.
_EXTRA_APT = (
    "pkg-config libssl-dev clang libclang-dev nodejs curl "
    "postgresql-15 postgresql-client-15 postgresql-server-dev-15 libicu-dev"
)
# .tree-sitter-cli-version at the base sha; CI uses tree-sitter/setup-action with this ref.
_TREE_SITTER_VERSION = "0.25.9"
# Pinned exactly as in the repo's Dockerfile (test DB image used by CI's sqlx jobs).
_PLPGSQL_CHECK_VERSION = "2.7.11"

# No extra packages: the test.patch crates are the ones the fix changes.
_EXTRA_PKG_DIRS: list[str] = []
# crates/pgls_semantic_tokens is created by fix.patch; its src/lib.rs carries unit tests
# authored by the fix itself (not by test.patch), which cannot be graded. For it only the
# integration-test targets that own test.patch files (tests/*.rs) are run.
_INTEGRATION_ONLY_PKG_DIRS = ["crates/pgls_semantic_tokens"]

# pgls_catalog's resolve::reference_tests are #[cfg(all(test, feature = "db"))]; CI's
# `cargo test --workspace` turns `db` on through feature unification (pgls_workspace
# depends on it), so a per-package run has to request it explicitly.
_PKG_FEATURES: dict[str, str] = {"crates/pgls_catalog": "db"}

_CARGO_FLAGS = "--offline"
_BUILD_TIMEOUT = "2400"
_RUN_TIMEOUT = "1500"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PKG_MARK = re.compile(r"^@@PKGDIR (\S+)$")
_SYNTH = re.compile(r"^@@SYNTH_FAIL (\S+)$")
_RUNNING = re.compile(r"^Running (?:unittests )?(\S+) \(")
_RESULT = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
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

# ---------- Section 2: PROVISION (build time, pinned by this commit's Cargo.lock) ----------
# crates/pgls_query vendors libpg_query as a submodule; build.rs compiles it.
git submodule update --init --recursive
cargo fetch --locked

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Compiles every test target of the packages the graded runs exercise, which also warms
# the target/ cache those runs reuse, and proves the test database comes up.
bash /home/start_pg.sh --check
bash /home/select_pkgs.sh
test -s /home/test_pkgs.txt
python3 /home/cargo_tests.py --gate
echo DEPS_OK
"""

# Starts the PG 15 cluster initialised in the base image (127.0.0.1:5432, postgres/postgres,
# plpgsql_check created in template1 + postgres, as the repo's test Dockerfile does).
# `--check` starts, probes and stops it again (build-time gate leaves no stale pid/socket).
_START_PG = """#!/bin/bash
set -eo pipefail

PGBIN=/usr/lib/postgresql/15/bin
PGDATA=/var/lib/pgls-pg/data
runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA" -l /var/lib/pgls-pg/server.log -w -t 120 start
PGPASSWORD=postgres psql -h 127.0.0.1 -U postgres -d postgres -Atc 'select version()'
if [ "${1:-}" = "--check" ]; then
    runuser -u postgres -- "$PGBIN/pg_ctl" -D "$PGDATA" -w -t 120 stop
fi
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
        if [ -f "$d/Cargo.toml" ] && grep -q '^\\[package\\]' "$d/Cargo.toml"; then
            echo "$d" >> "$out"
            break
        fi
        [ "$d" = "." ] && break
        d=$(dirname "$d")
    done
done < /home/test_files.txt
for d in [[EXTRA_PKG_DIRS]]; do
    if [ -f "$d/Cargo.toml" ]; then echo "$d" >> "$out"; fi
done
sort -u -o "$out" "$out"
echo "selected packages:"
cat "$out"
"""

# Runs `cargo test --tests` per selected package. If a package's test targets do not
# compile at this stage (typically: test.patch needs code from fix.patch), each target is
# compiled on its own; targets that still fail report every #[test] whose body test.patch
# touched as FAILED, so a compile break is graded as a failure instead of vanishing.
_CARGO_TESTS = r'''import json
import os
import re
import subprocess
import sys

REPO = "/home/[[REPO]]"
FLAGS = "[[CARGO_FLAGS]]".split()
GATE_TARGETS = ["--tests"]
INTEGRATION_ONLY = "[[INTEGRATION_ONLY_PKG_DIRS]]".split()
FEATURES = dict(
    item.split("=", 1) for item in "[[PKG_FEATURES]]".split(";") if "=" in item
)
BUILD_TIMEOUT = [[BUILD_TIMEOUT]]
RUN_TIMEOUT = [[RUN_TIMEOUT]]

ATTR = re.compile(r"^\s*#\[\s*(?:(?:[A-Za-z_]\w*::)*test\b|rstest\b|quickcheck\b)")
FN = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")
MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*\{")


def cargo(args, timeout):
    cmd = ["cargo", "test"] + FLAGS + args
    print("+ " + " ".join(cmd), flush=True)
    try:
        # stderr (cargo's "Running <target>" lines) shares stdout's fd so the target
        # header always precedes that target's test lines in the captured log.
        return subprocess.run(cmd, cwd=REPO, timeout=timeout, stderr=subprocess.STDOUT).returncode
    except subprocess.TimeoutExpired:
        print("CARGO_TIMEOUT " + " ".join(args), flush=True)
        return 124


def touched_lines():
    rows = {}
    with open("/home/test_targets.txt") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                rows.setdefault(parts[0], set()).add(int(parts[1]))
    return rows


def strip_code(line):
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])'", "''", line)
    return line.split("//", 1)[0]


def tests_in(path, prefix):
    try:
        with open(os.path.join(REPO, path)) as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    found = []
    mods = []
    depth = 0
    pending = None
    for idx, raw in enumerate(lines, start=1):
        code = strip_code(raw)
        if ATTR.match(code) and pending is None:
            pending = idx
        mod = MOD.match(code)
        fn = FN.match(code)
        if fn and pending is not None:
            start = pending
            end = idx
            level = 0
            opened = False
            for j in range(idx - 1, len(lines)):
                c = strip_code(lines[j])
                level += c.count("{") - c.count("}")
                if "{" in c:
                    opened = True
                if opened and level <= 0:
                    end = j + 1
                    break
            name = "::".join([p for p in prefix + [m for m, _ in mods] if p] + [fn.group(1)])
            found.append((name, start, end))
            pending = None
        elif fn:
            pending = None
        before = depth
        depth += code.count("{") - code.count("}")
        if mod:
            mods.append((mod.group(1), before))
        while mods and depth <= mods[-1][1]:
            mods.pop()
    return found


def path_decl(path):
    """(declaring file, module name) for a file pulled in via #[path = "..."] mod x;"""
    d = os.path.dirname(path)
    b = os.path.basename(path)
    try:
        names = sorted(os.listdir(os.path.join(REPO, d)))
    except OSError:
        return None
    for cand in names:
        if not cand.endswith(".rs") or cand == b:
            continue
        try:
            with open(os.path.join(REPO, d, cand)) as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for i, l in enumerate(lines):
            m = re.search(r'#\[path\s*=\s*"([^"]+)"\s*\]', l)
            if m and os.path.basename(m.group(1)) == b:
                for l2 in lines[i:i + 4]:
                    mm = re.search(r"\bmod\s+([A-Za-z_]\w*)\s*;", l2)
                    if mm:
                        return (d + "/" + cand if d else cand), mm.group(1)
    return None


def module_prefix(pkg, path, root):
    if path == root:
        return []
    decl = path_decl(path)
    if decl:
        return module_prefix(pkg, decl[0], root) + [decl[1]]
    src = pkg + "/src/" if pkg != "." else "src/"
    rel = path[len(src):] if path.startswith(src) else os.path.basename(path)
    parts = rel[:-3].split("/") if rel.endswith(".rs") else rel.split("/")
    if parts and parts[-1] == "mod":
        parts = parts[:-1]
    parts = [p.replace(".", "_") for p in parts]
    return parts


def synth(pkg, root, files, touched):
    for path in files:
        lines = touched.get(path, set())
        if not lines:
            continue
        for name, start, end in tests_in(path, module_prefix(pkg, path, root)):
            if any(start <= n <= end for n in lines):
                print("@@SYNTH_FAIL %s::%s" % (root, name), flush=True)


def pkg_files(pkg, touched):
    pre = "" if pkg == "." else pkg + "/"
    return sorted(p for p in touched if p.startswith(pre) and p.endswith(".rs"))


def lib_root(pkg):
    pre = "" if pkg == "." else pkg + "/"
    for cand in ("src/lib.rs", "src/main.rs"):
        if os.path.exists(os.path.join(REPO, pre + cand)):
            return pre + cand
    return pre + "src/lib.rs"


def pkg_args(pkg):
    manifest = "Cargo.toml" if pkg == "." else pkg + "/Cargo.toml"
    args = ["--manifest-path", manifest]
    if FEATURES.get(pkg):
        args += ["--features", FEATURES[pkg]]
    return args


def fallback(pkg, manifest, touched):
    files = pkg_files(pkg, touched)
    meta = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1", "--manifest-path", manifest] + FLAGS,
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    targets = []
    if meta.returncode == 0:
        absman = os.path.realpath(os.path.join(REPO, manifest))
        for p in json.loads(meta.stdout).get("packages", []):
            if os.path.realpath(p["manifest_path"]) != absman:
                continue
            for t in p.get("targets", []):
                kinds = t.get("kind", [])
                if not t.get("test", True):
                    continue
                src = os.path.relpath(os.path.realpath(t["src_path"]), os.path.realpath(REPO))
                if "lib" in kinds or "rlib" in kinds or "proc-macro" in kinds or "cdylib" in kinds or "staticlib" in kinds:
                    targets.append((["--lib"], src))
                elif "bin" in kinds:
                    targets.append((["--bin", t["name"]], src))
                elif "test" in kinds:
                    targets.append((["--test", t["name"]], src))
    if not targets:
        print("@@MANIFEST_BROKEN " + pkg, flush=True)
        claimed = set()
        for path in files:
            rel = path[len(pkg) + 1:] if pkg != "." else path
            if rel.startswith("tests/") and rel.count("/") == 1:
                synth(pkg, path, [path], touched)
                claimed.add(path)
        rest = [p for p in files if p not in claimed and "/src/" in "/" + p]
        if rest:
            synth(pkg, lib_root(pkg), rest, touched)
        return 1
    rc = 0
    for flag, src in targets:
        if pkg in INTEGRATION_ONLY and flag[0] != "--test":
            continue
        base = pkg_args(pkg) + flag
        if cargo(base + ["--no-run"], BUILD_TIMEOUT) == 0:
            r = cargo(base + ["--no-fail-fast"], RUN_TIMEOUT)
            rc = rc or r
            continue
        rc = 1
        print("@@TARGET_BUILD_FAILED %s %s" % (pkg, " ".join(flag)), flush=True)
        if flag[0] == "--test":
            tdir = os.path.dirname(src)
            mine = [p for p in files if p == src or (os.path.basename(src) == "main.rs" and p.startswith(tdir + "/"))]
        elif flag[0] == "--bin":
            mine = [p for p in files if p == src]
        else:
            pre = "" if pkg == "." else pkg + "/"
            mine = [p for p in files if p.startswith(pre + "src/") and not p.startswith(pre + "src/bin/")]
        synth(pkg, src, mine, touched)
    return rc


def gate(pkgs):
    """Build-time gate: every graded test target compiles from the locked graph."""
    for pkg in pkgs:
        r = cargo(["--locked"] + pkg_args(pkg) + GATE_TARGETS + ["--no-run"], BUILD_TIMEOUT)
        if r != 0:
            print("@@GATE_FAILED " + pkg, flush=True)
            return r
    return 0


def main():
    touched = touched_lines()
    with open("/home/test_pkgs.txt") as fh:
        pkgs = [l.strip() for l in fh if l.strip()]
    if "--gate" in sys.argv:
        return gate(pkgs)
    rc = 0
    for pkg in pkgs:
        manifest = "Cargo.toml" if pkg == "." else pkg + "/Cargo.toml"
        print("@@PKGDIR " + pkg, flush=True)
        targets = ["--tests"]
        if pkg in INTEGRATION_ONLY:
            targets = []
            for path in pkg_files(pkg, touched):
                rel = path[len(pkg) + 1:]
                if rel.startswith("tests/") and rel.count("/") == 1:
                    targets += ["--test", os.path.basename(rel)[:-3]]
            if not targets:
                continue
        if cargo(pkg_args(pkg) + targets + ["--no-run"], BUILD_TIMEOUT) == 0:
            r = cargo(pkg_args(pkg) + targets + ["--no-fail-fast"], RUN_TIMEOUT)
        else:
            r = fallback(pkg, manifest, touched)
        rc = rc or r
    print("@@CARGO_TESTS_EXIT %d" % rc, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
bash /home/start_pg.sh
bash /home/select_pkgs.sh
python3 /home/cargo_tests.py
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    found: dict[str, None] = {}
    for line in patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            found[header.group(2)] = None
    return list(found)


def _touched_lines(test_patch: str) -> str:
    """New-side line numbers each test.patch hunk adds or sits next to a removal."""
    rows: dict[str, None] = {}
    path: Optional[str] = None
    new_line = 0
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith(".rs") else None
            continue
        if path is None:
            continue
        hunk = _HUNK.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            rows[f"{path}\t{new_line}"] = None
            new_line += 1
        elif line.startswith("-"):
            rows[f"{path}\t{new_line}"] = None
        elif line.startswith(" ") or line == "":
            new_line += 1
    return "".join(f"{row}\n" for row in rows)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[CARGO_FLAGS]]", _CARGO_FLAGS)
        .replace("[[BUILD_TIMEOUT]]", _BUILD_TIMEOUT)
        .replace("[[RUN_TIMEOUT]]", _RUN_TIMEOUT)
        .replace("[[EXTRA_PKG_DIRS]]", " ".join(_EXTRA_PKG_DIRS))
        .replace("[[PKG_FEATURES]]", ";".join(f"{k}={v}" for k, v in _PKG_FEATURES.items()))
        .replace("[[INTEGRATION_ONLY_PKG_DIRS]]", " ".join(_INTEGRATION_ONLY_PKG_DIRS))
        .replace("[[REPO]]", pr.repo)
    )


class PostgresLanguageServer888To888ImageBase(Image):
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
        apt = " ".join(
            p for p in ["git ca-certificates build-essential python3", _EXTRA_APT] if p
        )
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV CARGO_TERM_COLOR=never \\
    CARGO_INCREMENTAL=0 \\
    CARGO_BUILD_JOBS=2 \\
    CARGO_NET_RETRY=5 \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    RUST_BACKTRACE=1 \\
    RUST_TEST_THREADS=2 \\
    PGPASSWORD=postgres \\
    DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \\
    SQLX_OFFLINE=true

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

RUN mkdir -p /etc/postgresql-common/createcluster.d && \\
    echo "create_main_cluster = false" > /etc/postgresql-common/createcluster.d/no-main.conf && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    {apt} \\
    && rm -rf /var/lib/apt/lists/*

# tree-sitter CLI, pinned to .tree-sitter-cli-version; upstream ships both arches.
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) ts=linux-x64 ;; \\
        arm64) ts=linux-arm64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/ts.gz "https://github.com/tree-sitter/tree-sitter/releases/download/v{_TREE_SITTER_VERSION}/tree-sitter-${{ts}}.gz"; \\
    gunzip /tmp/ts.gz; \\
    install -m 0755 /tmp/ts /usr/local/bin/tree-sitter; \\
    rm -f /tmp/ts; \\
    tree-sitter --version

# rust-toolchain.toml lists this target; installing it here keeps rustup off the network
# during the graded runs.
RUN rustup target add wasm32-unknown-emscripten && rustup show active-toolchain

# plpgsql_check, built against the packaged PG 15 server (same tag as the repo's Dockerfile).
RUN set -eux; \\
    curl -fsSL -o /tmp/plpgsql_check.tar.gz "https://github.com/okbob/plpgsql_check/archive/refs/tags/v{_PLPGSQL_CHECK_VERSION}.tar.gz"; \\
    mkdir -p /tmp/plpgsql_check; \\
    tar -xzf /tmp/plpgsql_check.tar.gz -C /tmp/plpgsql_check --strip-components=1; \\
    make -C /tmp/plpgsql_check USE_PGXS=1 PG_CONFIG=/usr/lib/postgresql/15/bin/pg_config -j2; \\
    make -C /tmp/plpgsql_check USE_PGXS=1 PG_CONFIG=/usr/lib/postgresql/15/bin/pg_config install; \\
    rm -rf /tmp/plpgsql_check /tmp/plpgsql_check.tar.gz

# Test cluster: postgres/postgres over TCP (sqlx::test reads DATABASE_URL), extensions in
# an `extensions` schema of template1 and postgres, mirroring the repo's test Dockerfile.
RUN set -eux; \\
    install -d -o postgres -g postgres /var/lib/pgls-pg; \\
    echo postgres > /tmp/pgpw; chown postgres /tmp/pgpw; \\
    runuser -u postgres -- /usr/lib/postgresql/15/bin/initdb -D /var/lib/pgls-pg/data -U postgres \\
        --pwfile=/tmp/pgpw --auth-local=trust --auth-host=scram-sha-256 -E UTF8 --locale=C.UTF-8; \\
    rm -f /tmp/pgpw; \\
    printf "listen_addresses = '127.0.0.1'\\nport = 5432\\nunix_socket_directories = '/tmp'\\nmax_connections = 200\\nfsync = off\\n" \\
        >> /var/lib/pgls-pg/data/postgresql.conf; \\
    runuser -u postgres -- /usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/pgls-pg/data -w start; \\
    for db in template1 postgres; do \\
        psql -h 127.0.0.1 -U postgres -d "$db" -v ON_ERROR_STOP=1 \\
            -c "CREATE SCHEMA IF NOT EXISTS extensions" \\
            -c "CREATE EXTENSION IF NOT EXISTS plpgsql_check SCHEMA extensions"; \\
    done; \\
    runuser -u postgres -- /usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/pgls-pg/data -w stop

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    rustc --version && cargo --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PostgresLanguageServer888To888ImageDefault(Image):
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
        return PostgresLanguageServer888To888ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        test_files = "".join(f"{f}\n" for f in _patch_files(pr.test_patch))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test_files.txt", test_files),
            File(".", "test_targets.txt", _touched_lines(pr.test_patch)),
            File(".", "start_pg.sh", _START_PG),
            File(".", "select_pkgs.sh", _render(_SELECT, pr)),
            File(".", "cargo_tests.py", _render(_CARGO_TESTS, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
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


@Instance.register("supabase-community", _INTERVAL)
class POSTGRES_LANGUAGE_SERVER_888_TO_888(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PostgresLanguageServer888To888ImageDefault(self.pr, self._config)

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
        pkg = "."
        root: Optional[str] = None
        for raw in clean.splitlines():
            line = raw.strip()
            mark = _PKG_MARK.match(line)
            if mark:
                pkg = mark.group(1)
                root = None
                continue
            synth = _SYNTH.match(line)
            if synth:
                failed_tests.add(synth.group(1))
                continue
            running = _RUNNING.match(line)
            if running:
                path = running.group(1)
                if pkg != "." and not path.startswith(pkg + "/"):
                    path = f"{pkg}/{path}"
                root = path
                continue
            result = _RESULT.match(line)
            if result and root:
                name = f"{root}::{result.group(1)}"
                status = result.group(2)
                if status == "ok":
                    passed_tests.add(name)
                elif status == "FAILED":
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
