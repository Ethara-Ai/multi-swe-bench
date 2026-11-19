import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "deeplethe"
_INTERVAL = "sandstorm_328_to_328"

# Toolchain pin: rust-toolchain.toml at the base sha (29179eec, 2026-10-24)
# says `channel = "stable"` and CI uses `dtolnay/rust-toolchain@stable`; stable
# on that date was 1.99.0. RUSTUP_TOOLCHAIN (base ENV) overrides the floating
# channel so rustup never resolves "stable" at test time.
# Mirror of the Docker Hub official image (same digests, no pull rate limit).
_RUST_VERSION = "1.99.0"
_RUST_IMAGE = f"public.ecr.aws/docker/library/rust:{_RUST_VERSION}-bookworm"
_PYTEST_VERSION = "8.4.2"
# rusqlite (bundled SQLite) and rcgen/aws-lc build C code from build scripts.
_EXTRA_APT = "clang libclang-dev cmake"

# Cargo workspace manifest (relative to the repo root).
_MANIFEST = "Cargo.toml"
# Era-mixed shards set this when some base commits carry no Cargo workspace.
_RUST_OPTIONAL = False
# The workspace gitignores Cargo.lock (.gitignore at the base sha: line 24),
# so no lockfile exists at the base commit. Resolving one at image-build time
# (`cargo generate-lockfile`) would drift with every rebuild, so the resolved
# graph is frozen here instead: _CARGO_LOCK (end of this file) is the lockfile
# `cargo generate-lockfile` produced for base 29179eec with Rust 1.99.0 on
# 2026-11-11 (264 packages), shipped as /home/Cargo.lock and checked against
# _CARGO_LOCK_SHA256 before `cargo fetch --locked`.
_GENERATE_LOCKFILE = False
_FROZEN_LOCKFILE = True
# Extra `cargo test` arguments and libtest arguments for every graded target.
_CARGO_TEST_ARGS = ""
_LIBTEST_ARGS = ""
_TARGET_TIMEOUT = 1800
# Per-PR pinned Python packages the PR's own Python tests import.
_PY_EXTRA: dict[int, list[str]] = {}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SYNTH = re.compile(r"^@@SYNTH_FAIL (\S+)$")
_MARK_CARGO = re.compile(r"^##MSB-CARGO (\S+)$")
_MARK_END = re.compile(r"^##MSB-(?:CARGO|PYTEST)-END\b")
_MARK_PYTEST = re.compile(r"^##MSB-PYTEST\b")
_LIBTEST = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)(?:,.*)?$")
_LIBTEST_FAILNAME = re.compile(r"^    (\S+)$")
_PYTEST = re.compile(
    r"^(\S+\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$"
)

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

# Test driver shared by the gate (prepare.sh) and the three graded stages.
# It maps every file test.patch adds/modifies onto real cargo test targets via
# `cargo metadata`, runs each target in its own `cargo test` invocation (so each
# libtest block is attributed to exactly one <crate>::<target>), and runs
# Python test files with pytest. A target that fails to compile, or a pytest
# file that fails to collect (typically: test.patch needs code from
# fix.patch), reports every test whose body test.patch adds/touches
# (/home/test_targets.txt: path<TAB>new-side line) as `@@SYNTH_FAIL <name>`
# under the exact name the passing stage prints, so the break is graded as a
# FAIL instead of vanishing (NONE). The remaining targets still run; the exit
# status is non-zero if any invocation failed.
_DRIVER = r'''#!/usr/bin/env python3
import ast
import json
import os
import re
import signal
import subprocess
import sys

REPO = "/home/@@REPO@@"
MANIFEST = "@@MANIFEST@@"
CARGO_TEST_ARGS = "@@CARGO_TEST_ARGS@@".split()
LIBTEST_ARGS = "@@LIBTEST_ARGS@@".split()
TIMEOUT = @@TIMEOUT@@
PYTHON = "/opt/msb-venv/bin/python"
NODEID = re.compile(r"^[^\s/][^\s]*\.py::")


def log(msg):
    print(msg, flush=True)


def wanted_files():
    with open("/home/test_files.txt") as fh:
        return [line.strip() for line in fh if line.strip()]


def run(cmd, cwd, env=None, timeout=TIMEOUT):
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        rc = 124
        err += ("\nMSB_TIMEOUT after %ss\n" % timeout).encode()
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


ATTR = re.compile(r"^\s*#\[\s*(?:(?:[A-Za-z_]\w*::)*test\b|rstest\b|quickcheck\b)")
FN = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")
MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*\{")


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


def rust_tests(path, prefix):
    """(libtest name, first line, last line) of every #[test]-like fn in path."""
    try:
        with open(path) as fh:
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
            start, end, level, opened = pending, idx, 0, False
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
    d, b = os.path.dirname(path), os.path.basename(path)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return None
    for cand in names:
        if not cand.endswith(".rs") or cand == b:
            continue
        try:
            with open(os.path.join(d, cand)) as fh:
                lines = fh.read().split("\n")
        except OSError:
            continue
        for i, l in enumerate(lines):
            m = re.search(r'#\[path\s*=\s*"([^"]+)"\s*\]', l)
            if m and os.path.basename(m.group(1)) == b:
                for l2 in lines[i:i + 4]:
                    mm = re.search(r"\bmod\s+([A-Za-z_]\w*)\s*;", l2)
                    if mm:
                        return os.path.join(d, cand), mm.group(1)
    return None


def module_prefix(path, root):
    """Module path of `path` inside the crate whose root file is `root`."""
    if path == root:
        return []
    decl = path_decl(path)
    if decl and decl[0] != path:
        return module_prefix(decl[0], root) + [decl[1]]
    rel = os.path.relpath(path, os.path.dirname(root))
    if rel.startswith(".."):
        return []
    parts = rel[:-3].split(os.sep) if rel.endswith(".rs") else rel.split(os.sep)
    if parts and parts[-1] == "mod":
        parts = parts[:-1]
    return [p.replace(".", "_").replace("-", "_") for p in parts]


def synth_rust(label, root, files, touched):
    for rel in files:
        lines = touched.get(rel, set())
        if not lines:
            continue
        path = os.path.join(REPO, rel)
        for name, start, end in rust_tests(path, module_prefix(path, root)):
            if any(start <= n <= end for n in lines):
                log("@@SYNTH_FAIL %s::%s" % (label, name))


def py_tests(path):
    """(pytest id suffix, first line, last line) of every test in a module."""
    try:
        with open(path) as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return []
    funcs = (ast.FunctionDef, ast.AsyncFunctionDef)

    def span(node):
        return min([node.lineno] + [d.lineno for d in node.decorator_list]), node.end_lineno

    found = []
    for node in tree.body:
        if isinstance(node, funcs) and node.name.startswith("test"):
            found.append((node.name,) + span(node))
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or node.bases):
            for sub in node.body:
                if isinstance(sub, funcs) and sub.name.startswith("test"):
                    found.append(("%s::%s" % (node.name, sub.name),) + span(sub))
    return found


def synth_py(rel, touched):
    lines = touched.get(rel, set())
    for name, start, end in py_tests(os.path.join(REPO, rel)):
        if any(start <= n <= end for n in lines):
            log("@@SYNTH_FAIL %s::%s" % (rel, name))


def cargo_targets(files):
    manifest = os.path.join(REPO, MANIFEST)
    rust = [f for f in files if f.endswith(".rs")]
    if not rust or not os.path.isfile(manifest):
        return [], []
    cwd = os.path.dirname(manifest)
    rc, out, err = run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"], cwd, timeout=600
    )
    if rc != 0:
        sys.stderr.write(err)
        raise SystemExit("cargo metadata failed")
    meta = json.loads(out)
    pkgs = []
    for pkg in meta["packages"]:
        pkgs.append((os.path.dirname(pkg["manifest_path"]), pkg))
    pkgs.sort(key=lambda item: len(item[0]), reverse=True)
    selected = {}
    owned = {}
    missing_pkgs = {}
    for rel in rust:
        path = os.path.join(REPO, rel)
        owner = next((p for d, p in pkgs if path.startswith(d + os.sep)), None)
        if owner is None:
            continue
        if not os.path.isfile(path):
            # Added by test.patch: not present at this stage. The gate still
            # compiles the owning package's test targets.
            missing_pkgs[owner["name"]] = None
            continue
        pdir = os.path.dirname(owner["manifest_path"])
        exact = [t for t in owner["targets"] if t["src_path"] == path]
        chosen = []
        if exact:
            chosen = exact
        elif path.startswith(os.path.join(pdir, "tests") + os.sep):
            chosen = [t for t in owner["targets"] if "test" in t["kind"]]
        elif path.startswith(os.path.join(pdir, "src") + os.sep):
            chosen = [t for t in owner["targets"] if "lib" in t["kind"] or "proc-macro" in t["kind"]]
        for t in chosen:
            if "test" in t["kind"]:
                flag = ["--test", t["name"]]
            elif "bin" in t["kind"]:
                flag = ["--bin", t["name"]]
            elif "lib" in t["kind"] or "proc-macro" in t["kind"] or "rlib" in t["kind"]:
                flag = ["--lib"]
            else:
                continue
            if not os.path.isfile(t["src_path"]):
                missing_pkgs[owner["name"]] = None
                continue
            label = "%s::%s" % (owner["name"], t["name"])
            selected[label] = (owner["name"], flag, cwd, t["src_path"])
            owned.setdefault(label, []).append(rel)
        if not chosen:
            missing_pkgs[owner["name"]] = None
    items = [(label, value + (owned[label],)) for label, value in selected.items()]
    return items, [(p, cwd) for p in missing_pkgs]


def python_groups(files):
    groups = {}
    for rel in files:
        base = os.path.basename(rel)
        if not rel.endswith(".py"):
            continue
        if not (base.startswith("test_") or base.endswith("_test.py")):
            continue
        path = os.path.join(REPO, rel)
        if not os.path.isfile(path):
            continue
        parent = os.path.dirname(path)
        project = os.path.dirname(parent) if os.path.basename(parent) in ("tests", "test") else parent
        groups.setdefault(project, []).append(path)
    return sorted(groups.items())


def pytest_cmd(paths, extra):
    return [
        PYTHON, "-m", "pytest", "-v", "-p", "no:cacheprovider", "-c", "/dev/null",
        "--rootdir", REPO, "-o", "console_output_style=classic",
    ] + extra + paths


def pyenv(project):
    env = dict(os.environ)
    env["PYTHONPATH"] = project + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def gate():
    files = wanted_files()
    targets, missing = cargo_targets(files)
    for label, (pkg, flag, cwd, _root, _owned) in targets:
        log("gate: compiling %s" % label)
        rc, out, err = run(["cargo", "test", "-p", pkg, "--no-run"] + flag + CARGO_TEST_ARGS, cwd, timeout=7200)
        if rc != 0:
            sys.stderr.write(err[-20000:])
            raise SystemExit("gate failed for %s" % label)
    for pkg, cwd in missing:
        log("gate: compiling every test target of %s (new test file at this commit)" % pkg)
        rc, out, err = run(["cargo", "test", "-p", pkg, "--no-run", "--tests"] + CARGO_TEST_ARGS, cwd, timeout=7200)
        if rc != 0:
            sys.stderr.write(err[-20000:])
            raise SystemExit("gate failed for package %s" % pkg)
    rc, out, err = run([PYTHON, "-c", "import pytest; print(pytest.__version__)"], REPO, timeout=300)
    if rc != 0:
        sys.stderr.write(err)
        raise SystemExit("gate failed: pytest is not importable")
    for project, paths in python_groups(files):
        log("gate: collecting %s" % " ".join(paths))
        rc, out, err = run(pytest_cmd(paths, ["--collect-only", "-q"]), project, env=pyenv(project), timeout=900)
        if rc != 0:
            sys.stdout.write(out[-20000:])
            sys.stderr.write(err[-20000:])
            raise SystemExit("gate failed: pytest collection of %s" % project)
    log("DEPS_OK")


def run_all():
    files = wanted_files()
    touched = touched_lines()
    status = 0
    targets, _ = cargo_targets(files)
    for label, (pkg, flag, cwd, root, owned) in targets:
        log("##MSB-CARGO %s" % label)
        build = ["cargo", "test", "-p", pkg] + flag + CARGO_TEST_ARGS
        rc, out, err = run(build + ["--no-run"], cwd)
        if rc != 0:
            sys.stdout.write(err)
            sys.stdout.write(out)
            log("@@TARGET_BUILD_FAILED %s" % label)
            synth_rust(label, root, owned, touched)
            log("##MSB-CARGO-END rc=%d" % rc)
            status = status or rc
            continue
        cmd = build + ["--", "--color", "never"] + LIBTEST_ARGS
        rc, out, err = run(cmd, cwd)
        sys.stdout.write(err)
        sys.stdout.write(out)
        log("##MSB-CARGO-END rc=%d" % rc)
        status = status or rc
    for project, paths in python_groups(files):
        log("##MSB-PYTEST %s" % project)
        rc, out, err = run(
            pytest_cmd(paths, ["-rA", "--continue-on-collection-errors"]),
            project,
            env=pyenv(project),
        )
        # pytest prints node ids relative to its working directory (the
        # project dir, which the suites expect); re-anchor them on the repo
        # root so names are stable and map onto test.patch paths.
        rel = os.path.relpath(project, REPO)
        if rel != ".":
            out = "".join(
                rel + "/" + line if NODEID.match(line) else line
                for line in out.splitlines(True)
            )
        sys.stdout.write(err)
        sys.stdout.write(out)
        if rc != 0:
            # A module that failed to collect (or a crashed run) prints no
            # node id for any of its tests: report its touched tests failed.
            for path in paths:
                rel_path = os.path.relpath(path, REPO)
                if not any(line.startswith(rel_path + "::") for line in out.splitlines()):
                    log("@@TARGET_COLLECT_FAILED %s" % rel_path)
                    synth_py(rel_path, touched)
        log("##MSB-PYTEST-END rc=%d" % rc)
        status = status or rc
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    if sys.argv[1:] == ["gate"]:
        gate()
    else:
        raise SystemExit(run_all())
'''

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, pinned by this commit's Cargo.lock) ----------
@@CARGO_PROVISION@@
@@PY_PROVISION@@
# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Compiles every cargo test target this PR's test files map onto (or, for a
# test file the PR adds, every test target of its package) and collects the
# PR's Python tests, which also warms the build cache the graded runs reuse.
python3 /home/msb_tests.py gate
"""

_CARGO_PROVISION = """cd "$(dirname /home/@@REPO@@/@@MANIFEST@@)"
@@LOCKGEN@@cargo fetch --locked
cd /home/@@REPO@@
"""

_LOCKGEN = """# This workspace gitignores Cargo.lock, so resolve once here at build time;
# the generated lockfile (untracked) then pins every graded stage.
cargo generate-lockfile
"""

# WHY: the repo gitignores Cargo.lock, so the base commit carries none. This is
# environment pinning (the dependency graph the validated build resolved),
# not part of the fix: the file is ignored by git and the tree stays clean.
_LOCKCOPY = """# Repo gitignores Cargo.lock: install the frozen, validated resolution
# (environment only, ignored by git) so every rebuild uses the same crates.
cp /home/Cargo.lock ./Cargo.lock
echo "@@LOCKSHA@@  Cargo.lock" | sha256sum -c -
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/@@REPO@@
@@APPLY@@
python3 /home/msb_tests.py run
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _test_files(test_patch: str) -> list[str]:
    """Files test.patch leaves present (added or modified, not deleted)."""
    found: dict[str, None] = {}
    current: str | None = None
    deleted = False
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            if current and not deleted:
                found[current] = None
            current = header.group(2)
            deleted = False
            continue
        if current and line.startswith("deleted file mode"):
            deleted = True
        if current and line.startswith("+++ /dev/null"):
            deleted = True
    if current and not deleted:
        found[current] = None
    return list(found)


def _touched_lines(test_patch: str) -> str:
    """New-side line numbers each test.patch hunk adds, or sits next to a
    removal, in .rs/.py files (path<TAB>line rows for the driver's synthesis)."""
    rows: dict[str, None] = {}
    path: str | None = None
    new_line = 0
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith((".rs", ".py")) else None
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


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("@@REPO@@", pr.repo)
        .replace("@@MANIFEST@@", _MANIFEST)
        .replace("@@CARGO_TEST_ARGS@@", _CARGO_TEST_ARGS)
        .replace("@@LIBTEST_ARGS@@", _LIBTEST_ARGS)
        .replace("@@TIMEOUT@@", str(_TARGET_TIMEOUT))
    )


class Sandstorm328To328ImageBase(Image):
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
            p
            for p in [
                "git ca-certificates build-essential pkg-config libssl-dev "
                "python3 python3-venv",
                _EXTRA_APT,
            ]
            if p
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

ENV RUSTUP_TOOLCHAIN={_RUST_VERSION} \\
    CARGO_HTTP_CAINFO=${{CA_CERT_PATH}} \\
    CARGO_NET_RETRY=10 \\
    CARGO_BUILD_JOBS=2 \\
    CARGO_INCREMENTAL=0 \\
    CARGO_TERM_COLOR=never \\
    CARGO_PROFILE_DEV_DEBUG=0 \\
    CARGO_PROFILE_TEST_DEBUG=0 \\
    RUST_BACKTRACE=1 \\
    PIP_CERT=${{CA_CERT_PATH}} \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONUNBUFFERED=1

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
    {apt} \\
    && rm -rf /var/lib/apt/lists/*

# Shard-wide test tooling: pytest for any Python test file a PR touches.
RUN python3 -m venv /opt/msb-venv && \\
    /opt/msb-venv/bin/pip install --no-cache-dir "pytest=={_PYTEST_VERSION}" && \\
    /opt/msb-venv/bin/python -m pytest --version

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


class Sandstorm328To328ImageDefault(Image):
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
        return Sandstorm328To328ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        pr = self.pr
        cargo = _render(_CARGO_PROVISION, pr).replace(
            "@@LOCKGEN@@",
            _LOCKGEN if _GENERATE_LOCKFILE
            else _LOCKCOPY.replace("@@LOCKSHA@@", _CARGO_LOCK_SHA256) if _FROZEN_LOCKFILE
            else "",
        )
        if _RUST_OPTIONAL:
            body = "".join(f"    {line}\n" if line else "\n" for line in cargo.splitlines())
            cargo = (
                "# Era-mixed shard: some base commits predate the Cargo workspace.\n"
                f"if [ -f /home/{pr.repo}/{_MANIFEST} ]; then\n{body}fi\n"
            )
        extra = _PY_EXTRA.get(pr.number, [])
        py = ""
        if extra:
            pins = " ".join(f'"{p}"' for p in extra)
            mods = ", ".join(p.split("==")[0].replace("-", "_") for p in extra)
            py = (
                "# This PR's Python tests import the packages below. Their pins are the\n"
                "# PR's own declared versions; the manifest declaring them is introduced by\n"
                "# the PR itself, so they are mirrored here for the image to be complete.\n"
                f"/opt/msb-venv/bin/pip install --no-cache-dir {pins}\n"
                f'/opt/msb-venv/bin/python -c "import {mods}"\n'
            )
        return (
            _render(_PREPARE, pr)
            .replace("@@SHA@@", pr.base.sha)
            .replace("@@CARGO_PROVISION@@", cargo)
            .replace("@@PY_PROVISION@@", py)
        )

    def files(self) -> list[File]:
        pr = self.pr
        test_files = "".join(f"{f}\n" for f in _test_files(pr.test_patch))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "test_files.txt", test_files),
            File(".", "test_targets.txt", _touched_lines(pr.test_patch)),
            File(".", "Cargo.lock", _CARGO_LOCK),
            File(".", "msb_tests.py", _render(_DRIVER, pr)),
            File(".", "prepare.sh", self._prepare()),
            File(".", "run.sh", _render(_RUN, pr).replace("@@APPLY@@", "")),
            File(".", "test-run.sh", _render(_RUN, pr).replace("@@APPLY@@", _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr).replace("@@APPLY@@", _APPLY_FIX)),
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


@Instance.register(_ORG, _INTERVAL)
class SANDSTORM_328_TO_328(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Sandstorm328To328ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        label: str | None = None
        in_pytest = False
        segment: list[str] = []

        def flush_failures() -> None:
            # libtest's closing "failures:" list names every failed test even
            # when a child process garbled the "test x ... FAILED" line.
            if label is None:
                return
            last = None
            for i, text in enumerate(segment):
                if text == "failures:":
                    last = i
            if last is None:
                return
            for text in segment[last + 1:]:
                if text.startswith("test result:"):
                    break
                match = _LIBTEST_FAILNAME.match(text)
                if match:
                    failed_tests.add(f"{label}::{match.group(1)}")

        for raw in clean.split("\n"):
            line = raw.rstrip()
            synth = _SYNTH.match(line)
            if synth:
                failed_tests.add(synth.group(1))
                continue
            mark = _MARK_CARGO.match(line)
            if mark:
                flush_failures()
                label, in_pytest, segment = mark.group(1), False, []
                continue
            if _MARK_PYTEST.match(line):
                flush_failures()
                label, in_pytest, segment = None, True, []
                continue
            if _MARK_END.match(line):
                flush_failures()
                label, in_pytest, segment = None, False, []
                continue
            if label is not None:
                segment.append(line)
                result = _LIBTEST.match(line)
                if result:
                    name = f"{label}::{result.group(1)}"
                    status = result.group(2)
                    if status == "ok":
                        passed_tests.add(name)
                    elif status == "FAILED":
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)
                continue
            if in_pytest:
                result = _PYTEST.match(line)
                if result:
                    name, status = result.group(1), result.group(2)
                    if status in ("PASSED", "XPASS"):
                        passed_tests.add(name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(name)
                    else:
                        skipped_tests.add(name)
        flush_failures()
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


# Frozen Cargo.lock for base 29179eec (see _FROZEN_LOCKFILE above).
_CARGO_LOCK_SHA256 = "0758b470a6b67cc61cbd94ee0315afb1fd85e29046e52bf8a509937b5ee3f835"
_CARGO_LOCK = r"""# This file is automatically @generated by Cargo.
# It is not intended for manual editing.
version = 4

[[package]]
name = "adler2"
version = "2.0.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "320119579fcad9c21884f5c4861d16174d0e06250625266f50fe6898340abefa"

[[package]]
name = "ahash"
version = "0.8.12"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5a15f179cd60c4584b8a8c596927aadc462e27f2ca70c04e0071964a73ba7a75"
dependencies = [
 "cfg-if",
 "once_cell",
 "version_check",
 "zerocopy",
]

[[package]]
name = "aho-corasick"
version = "1.1.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c982642fa9e8606056828ee9a8505737230110bb1099153c79efe865c59d12ba"
dependencies = [
 "memchr",
]

[[package]]
name = "anstream"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "824a212faf96e9acacdbd09febd34438f8f711fb84e09a8916013cd7815ca28d"
dependencies = [
 "anstyle",
 "anstyle-parse",
 "anstyle-query",
 "anstyle-wincon",
 "colorchoice",
 "is_terminal_polyfill",
 "utf8parse",
]

[[package]]
name = "anstyle"
version = "1.0.14"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "940b3a0ca603d1eade50a4846a2afffd5ef57a9feac2c0e2ec2e14f9ead76000"

[[package]]
name = "anstyle-parse"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "52ce7f38b242319f7cabaa6813055467063ecdc9d355bbb4ce0c68908cd8130e"
dependencies = [
 "utf8parse",
]

[[package]]
name = "anstyle-query"
version = "1.1.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "40c48f72fd53cd289104fc64099abca73db4166ad86ea0b4341abe65af83dadc"
dependencies = [
 "windows-sys 0.61.2",
]

[[package]]
name = "anstyle-wincon"
version = "3.0.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "291e6a250ff86cd4a820112fb8898808a366d8f9f58ce16d1f538353ad55747d"
dependencies = [
 "anstyle",
 "once_cell_polyfill",
 "windows-sys 0.61.2",
]

[[package]]
name = "anyhow"
version = "1.0.104"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "330a5ed07fa54e4702c9d6c4174f74427fc0ef6e214bbd677ae50a5099946470"

[[package]]
name = "arc-swap"
version = "1.9.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "84a22b5d51209f6de83d37a0b33d320acb8da88823e42a94c075140708f032a5"
dependencies = [
 "rustversion",
]

[[package]]
name = "async-trait"
version = "0.1.92"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "82f6aeea286b8eb4dd3431a1be1b59d290ace00f5bfd8e2a159bc2a05e2c1667"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "atomic-waker"
version = "1.1.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1505bd5d3d116872e7271a6d4e16d81d0c8570876c8de68093a09ac269d8aac0"

[[package]]
name = "autocfg"
version = "1.5.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f2032f911046de80f0a198e0901378627c33f59ea0ac00e363d481118bd70a53"

[[package]]
name = "aws-lc-rs"
version = "1.19.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "048006790404103c488e6c5616b3e7e702a71292aadda08e8f7698a9e0d2eb53"
dependencies = [
 "aws-lc-sys",
 "zeroize",
]

[[package]]
name = "aws-lc-sys"
version = "0.47.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8bfbe32ac36b3c428696ce315464181bc4029d689fd58e3f2a4e58c252242f52"
dependencies = [
 "cc",
 "cmake",
 "dunce",
 "fs_extra",
 "pkg-config",
]

[[package]]
name = "axum"
version = "0.7.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "edca88bc138befd0323b20752846e6587272d3b03b0343c8ea28a6f819e6e71f"
dependencies = [
 "async-trait",
 "axum-core",
 "bytes",
 "futures-util",
 "http",
 "http-body",
 "http-body-util",
 "hyper",
 "hyper-util",
 "itoa",
 "matchit",
 "memchr",
 "mime",
 "percent-encoding",
 "pin-project-lite",
 "rustversion",
 "serde",
 "serde_json",
 "serde_path_to_error",
 "serde_urlencoded",
 "sync_wrapper",
 "tokio",
 "tower",
 "tower-layer",
 "tower-service",
 "tracing",
]

[[package]]
name = "axum-core"
version = "0.4.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "09f2bd6146b97ae3359fa0cc6d6b376d9539582c7b4220f041a33ec24c226199"
dependencies = [
 "async-trait",
 "bytes",
 "futures-util",
 "http",
 "http-body",
 "http-body-util",
 "mime",
 "pin-project-lite",
 "rustversion",
 "sync_wrapper",
 "tower-layer",
 "tower-service",
 "tracing",
]

[[package]]
name = "axum-server"
version = "0.8.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b1df331683d982a0b9492b38127151e6453639cd34926eb9c07d4cd8c6d22bfc"
dependencies = [
 "arc-swap",
 "bytes",
 "either",
 "fs-err",
 "http",
 "http-body",
 "hyper",
 "hyper-util",
 "pin-project-lite",
 "rustls",
 "rustls-pki-types",
 "tokio",
 "tokio-rustls",
 "tower-service",
]

[[package]]
name = "base64"
version = "0.22.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "72b3254f16251a8381aa12e40e3c4d2f0199f8c6508fbecb9d91f575e0fbb8c6"

[[package]]
name = "base64"
version = "0.23.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ac07cdecf99051d9a5238b80f35af32cdeba5b336e55d957b318b50137e18da5"

[[package]]
name = "bitflags"
version = "1.3.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bef38d45163c2f1dde094a7dfd33ccf595c92905c8f8f4fdc18d06fb1037718a"

[[package]]
name = "bitflags"
version = "2.13.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3ded4057c258ba199e2d26386d3af3780957ecaee6c4ef4041c6b4b8b97c0b06"

[[package]]
name = "block-buffer"
version = "0.10.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3078c7629b62d3f0439517fa394996acacc5cbc91c5a20d8c658e77abd503a71"
dependencies = [
 "generic-array",
]

[[package]]
name = "bumpalo"
version = "3.20.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "72f5acc6cb2ba439de613abc23857ec3d78374d8ed5ac84e9d11336e87da8649"

[[package]]
name = "bytes"
version = "1.12.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "fc652a48c352aef3ea3aed32080501cf3ef6ed5da78602a020c991775b0aff04"

[[package]]
name = "cc"
version = "1.11.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4ff2a8126fff0f8c839080d619675a585e61720bb12d2baceae45769fd66f155"
dependencies = [
 "find-msvc-tools",
 "jobserver",
 "libc",
 "shlex",
]

[[package]]
name = "cfg-if"
version = "1.0.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4e7648175b45a9a48536d676f68d918270699102aa8dab5496df06904c914600"

[[package]]
name = "cfg_aliases"
version = "0.2.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f079e83a288787bcd14a6aea84cee5c87a67c5a3e660c30f557a3d24761b3527"

[[package]]
name = "chacha20"
version = "0.10.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "65c35e4b699c7e15ccbe7ee35c005e4fc0a278d22238a2857e6ce2dadeda1b06"
dependencies = [
 "cfg-if",
 "cpufeatures 0.3.1",
 "rand_core 0.10.1",
]

[[package]]
name = "clap"
version = "4.6.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "aa8876b300ab35ba921adea3dfd70157a46249b33f95c9084ae5709785478946"
dependencies = [
 "clap_builder",
 "clap_derive",
]

[[package]]
name = "clap_builder"
version = "4.6.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ec0797fb7aeb1406c84efac526901f7ec3ead2124f946b494e72879d4b54704d"
dependencies = [
 "anstream",
 "anstyle",
 "clap_lex",
 "strsim",
]

[[package]]
name = "clap_derive"
version = "4.6.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f9c751b79415d4e559e3d1fcf128e09e720eb673a06d26cf6f392d37d75b66e0"
dependencies = [
 "heck",
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "clap_lex"
version = "1.1.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1c133bc6a41be0d194c306b5506d15e6feeea7b1d6604bd3f8310dfb2ca96486"

[[package]]
name = "cmake"
version = "0.1.58"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c0f78a02292a74a88ac736019ab962ece0bc380e3f977bf72e376c5d78ff0678"
dependencies = [
 "cc",
]

[[package]]
name = "colorchoice"
version = "1.0.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1d07550c9036bf2ae0c684c4297d503f838287c83c53686d05370d0e139ae570"

[[package]]
name = "cpufeatures"
version = "0.2.17"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "59ed5838eebb26a2bb2e58f6d5b5316989ae9d08bab10e0e6d103e656d1b0280"
dependencies = [
 "libc",
]

[[package]]
name = "cpufeatures"
version = "0.3.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5ca28b0ae3115b884660db4118d803791fd6756b6e88f39c0f3f7859060d7566"
dependencies = [
 "libc",
]

[[package]]
name = "crc32fast"
version = "1.5.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2b929dd2a135771f40afabd1073f85eed59c800b7c61901e5c254270497dad64"
dependencies = [
 "cfg-if",
]

[[package]]
name = "crypto-common"
version = "0.1.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "78c8292055d1c1df0cce5d180393dc8cce0abec0a7102adb6c7b1eef6016d60a"
dependencies = [
 "generic-array",
 "typenum",
]

[[package]]
name = "deranged"
version = "0.5.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9e9de72ce2ad1f90dc62fa25f0f430ef85eb4b0d8fa0be4f30373bc40a21d28e"

[[package]]
name = "digest"
version = "0.10.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9ed9a281f7bc9b7576e61468ba615a66a5c8cfdff42420a70aa82701a3b1e292"
dependencies = [
 "block-buffer",
 "crypto-common",
]

[[package]]
name = "displaydoc"
version = "0.2.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c6232dd377dcc64799954cbd3a9bb882e9cdc1308ccd87b1c098f1fb2eaf82a8"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "dunce"
version = "1.0.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "92773504d58c093f6de2459af4af33faa518c13451eb8f2b5698ed3d36e7c813"

[[package]]
name = "either"
version = "1.19.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0e9c71c2167ca323c882b99918929403426e2373ea17242ff5653e0d5e1058be"

[[package]]
name = "equivalent"
version = "1.0.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "00d174d5400e5e8fd687ad1049e2f578285fa914201b1af7e8b112a4546bd826"

[[package]]
name = "errno"
version = "0.3.14"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "39cab71617ae0d63f51a36d69f866391735b51691dbda63cf6f96d042b63efeb"
dependencies = [
 "libc",
 "windows-sys 0.61.2",
]

[[package]]
name = "fallible-iterator"
version = "0.3.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2acce4a10f12dc2fb14a218589d4f1f62ef011b2d0cc4b3cb1bba8e94da14649"

[[package]]
name = "fallible-streaming-iterator"
version = "0.1.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7360491ce676a36bf9bb3c56c1aa791658183a54d2744120f27285738d90465a"

[[package]]
name = "fastrand"
version = "2.5.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "da7c62ceae207dd37ea5b845da6a0696c799f85e97da1ab5b7910be3c1c80223"

[[package]]
name = "filetime"
version = "0.2.29"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5c287a33c7f0a620c38e641e7f60827713987b3c0f26e8ddc9462cc69cf75759"
dependencies = [
 "cfg-if",
 "libc",
]

[[package]]
name = "find-msvc-tools"
version = "0.1.15"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1c66915cc5391097de7a2b0b972e8505aa33c0df401fd3a6811dbdb32881ba48"

[[package]]
name = "flate2"
version = "1.1.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "626d4928326b646e70f65ed36fc7cf9f206bdb68a0019ee7c6b4709da4e084c4"
dependencies = [
 "crc32fast",
 "miniz_oxide",
 "zlib-rs",
]

[[package]]
name = "fnv"
version = "1.0.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3f9eec918d3f24069decb9af1554cad7c880e2da24a9afd88aca000531ab82c1"

[[package]]
name = "forkd-cli"
version = "0.5.3"
dependencies = [
 "anyhow",
 "clap",
 "forkd-uffd",
 "forkd-vmm",
 "libc",
 "serde",
 "serde_json",
 "sha2",
 "tar",
 "tempfile",
 "toml",
 "tracing",
 "tracing-subscriber",
 "ureq",
 "zstd",
]

[[package]]
name = "forkd-controller"
version = "0.5.3"
dependencies = [
 "anyhow",
 "axum",
 "axum-server",
 "clap",
 "forkd-uffd",
 "forkd-vmm",
 "libc",
 "parking_lot",
 "rcgen",
 "reqwest",
 "rustls",
 "serde",
 "serde_json",
 "subtle",
 "tempfile",
 "thiserror",
 "time",
 "tokio",
 "tower",
 "tracing",
 "tracing-subscriber",
]

[[package]]
name = "forkd-core"
version = "0.5.3"
dependencies = [
 "anyhow",
 "axum",
 "clap",
 "hex",
 "parking_lot",
 "rand 0.8.8",
 "rcgen",
 "reqwest",
 "rusqlite",
 "serde",
 "serde_json",
 "subtle",
 "tempfile",
 "thiserror",
 "time",
 "tokio",
 "tracing",
 "tracing-subscriber",
 "uuid",
]

[[package]]
name = "forkd-uffd"
version = "0.5.3"
dependencies = [
 "anyhow",
 "clap",
 "libc",
 "serde",
 "serde_json",
 "tracing",
 "tracing-subscriber",
]

[[package]]
name = "forkd-vmm"
version = "0.5.3"
dependencies = [
 "anyhow",
 "forkd-uffd",
 "libc",
 "serde",
 "serde_json",
 "sha2",
 "thiserror",
 "tokio",
 "tracing",
]

[[package]]
name = "form_urlencoded"
version = "1.2.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cb4cb245038516f5f85277875cdaa4f7d2c9a0fa0468de06ed190163b1581fcf"
dependencies = [
 "percent-encoding",
]

[[package]]
name = "fs-err"
version = "3.3.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b5c95b673b8f6f7235229ae11c5642d81b04c2e64c1e2fb417bc0cf73ca45f29"
dependencies = [
 "autocfg",
 "tokio",
]

[[package]]
name = "fs_extra"
version = "1.3.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "42703706b716c37f96a77aea830392ad231f44c9e9a67872fa5548707e11b11c"

[[package]]
name = "futures-channel"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b1f9e3d69d39e4862ffed03ed071a76f9a13ba1d9109d355b0f0aa6b15e393c4"
dependencies = [
 "futures-core",
]

[[package]]
name = "futures-core"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "92d699e522242e69e3003b94ecc1f960f3a5e015aa7c5d7486e65ad01dd94f5e"

[[package]]
name = "futures-sink"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1944426bf7d03f1d14f708785e4b33efd750b36d48a157b836b3efc15ede8e1d"

[[package]]
name = "futures-task"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cd417de3d1d015fc3bfd2b1ea46dfc7bab72ef86f1cc7cc9c78e728b34a6d1fd"

[[package]]
name = "futures-util"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0d50a92467f8ba5dd6e3ee5d4bd04d73ab2e4e1c44474a0674821dfce14b79bc"
dependencies = [
 "futures-core",
 "futures-task",
 "pin-project-lite",
 "slab",
]

[[package]]
name = "generic-array"
version = "0.14.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "85649ca51fd72272d7821adaf274ad91c288277713d9c18820d8499a7ff69e9a"
dependencies = [
 "typenum",
 "version_check",
]

[[package]]
name = "getrandom"
version = "0.2.17"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ff2abc00be7fca6ebc474524697ae276ad847ad0a6b3faa4bcb027e9a4614ad0"
dependencies = [
 "cfg-if",
 "js-sys",
 "libc",
 "wasi",
 "wasm-bindgen",
]

[[package]]
name = "getrandom"
version = "0.4.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "300e883d756b2e4ec94e02791f39b04b522276138852cfc41d9fb7e904106099"
dependencies = [
 "cfg-if",
 "js-sys",
 "libc",
 "r-efi",
 "rand_core 0.10.1",
 "wasm-bindgen",
]

[[package]]
name = "h2"
version = "0.4.20"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7d29020232d6aa3fb1daca64c1127cf662cf97f254ae16c18c05b8ab635fc118"
dependencies = [
 "atomic-waker",
 "bytes",
 "fnv",
 "futures-core",
 "futures-sink",
 "http",
 "indexmap",
 "slab",
 "tokio",
 "tokio-util",
 "tracing",
]

[[package]]
name = "hashbrown"
version = "0.14.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e5274423e17b7c9fc20b6e7e208532f9b19825d82dfd615708b70edd83df41f1"
dependencies = [
 "ahash",
]

[[package]]
name = "hashbrown"
version = "0.17.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ed5909b6e89a2db4456e54cd5f673791d7eca6732202bbf2a9cc504fe2f9b84a"

[[package]]
name = "hashlink"
version = "0.9.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6ba4ff7128dee98c7dc9794b6a411377e1404dba1c97deb8d1a55297bd25d8af"
dependencies = [
 "hashbrown 0.14.5",
]

[[package]]
name = "heck"
version = "0.5.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2304e00983f87ffb38b55b444b5e3b60a884b5d30c0fca7d82fe33449bbe55ea"

[[package]]
name = "hex"
version = "0.4.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7f24254aa9a54b5c858eaee2f5bccdb46aaf0e486a595ed5fd8f86ba55232a70"

[[package]]
name = "http"
version = "1.5.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "918d3568bebf352712bc2ef3d46a8bcf1a75b373be6539de198e9105cbbf9ce0"
dependencies = [
 "bytes",
 "itoa",
]

[[package]]
name = "http-body"
version = "1.1.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ca2a8f2913ee65f60facd6a5905613afaa448497a0230cc41ce022d93290bc2c"
dependencies = [
 "bytes",
 "http",
]

[[package]]
name = "http-body-util"
version = "0.1.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "23169fe34a5fbcdd3f3862e78fb9b6fccd5f02a6dc6f732547005d45631ce71c"
dependencies = [
 "bytes",
 "futures-core",
 "http",
 "http-body",
 "pin-project-lite",
]

[[package]]
name = "httparse"
version = "1.10.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6dbf3de79e51f3d586ab4cb9d5c3e2c14aa28ed23d180cf89b4df0454a69cc87"

[[package]]
name = "httpdate"
version = "1.0.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "df3b46402a9d5adb4c86a0cf463f42e19994e3ee891101b1841f30a545cb49a9"

[[package]]
name = "hyper"
version = "1.12.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2c3e324da4c95177d6291d4c8730197c0d1822f8a9766814a4a44fa5ab797c9c"
dependencies = [
 "atomic-waker",
 "bytes",
 "futures-channel",
 "futures-core",
 "h2",
 "http",
 "http-body",
 "httparse",
 "httpdate",
 "itoa",
 "pin-project-lite",
 "smallvec",
 "tokio",
 "want",
]

[[package]]
name = "hyper-rustls"
version = "0.27.10"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "dfa8e654703247911e29c23fbeaa261834bd9bb74efba2f9acddc37bfb127f53"
dependencies = [
 "http",
 "hyper",
 "hyper-util",
 "rustls",
 "tokio",
 "tokio-rustls",
 "tower-service",
 "webpki-roots 1.0.12",
]

[[package]]
name = "hyper-util"
version = "0.1.21"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ddc03d96684f9226b8a787cdb71488417b53ab5ea8fdb1dac946cb9431cc8bff"
dependencies = [
 "base64 0.23.1",
 "bytes",
 "futures-channel",
 "futures-util",
 "http",
 "http-body",
 "httparse",
 "hyper",
 "ipnet",
 "libc",
 "percent-encoding",
 "pin-project-lite",
 "socket2",
 "tokio",
 "tower-service",
 "tracing",
]

[[package]]
name = "icu_collections"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "fe090a42a0d3140fed8c63263ed1fecc02422cb2f4bfe55cd8de2de1b8e105dd"
dependencies = [
 "displaydoc",
 "potential_utf",
 "utf8_iter",
 "yoke",
 "zerofrom",
 "zerovec",
]

[[package]]
name = "icu_locale_core"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "25d29b57c17c6528d0dc4c2141cfa2d08200465acb537a3909c8b9b8a4a984c0"
dependencies = [
 "displaydoc",
 "litemap",
 "tinystr",
 "writeable",
 "zerovec",
]

[[package]]
name = "icu_normalizer"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "404a3b8c486f205c87fe7f23ab6894d84a4ad76d6e4671ae1981ba7def87c599"
dependencies = [
 "icu_collections",
 "icu_normalizer_data",
 "icu_properties",
 "icu_provider",
 "smallvec",
 "zerovec",
]

[[package]]
name = "icu_normalizer_data"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "20f0088bfb7441f7a37fa0c9e20cb1e9cba1cac6b9ce8cd2a6dc44294f8e8cbc"

[[package]]
name = "icu_properties"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2ea83d991c56158f7cf898bf86436c1dfe7c07ee82fe88951f2695c9f37a379f"
dependencies = [
 "displaydoc",
 "icu_collections",
 "icu_locale_core",
 "icu_properties_data",
 "icu_provider",
 "zerotrie",
 "zerovec",
]

[[package]]
name = "icu_properties_data"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b98f29af82d02b163fe50161388350e743bb3107cc8e8c2f7314cb505faddfcf"

[[package]]
name = "icu_provider"
version = "2.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "413099315636c588ee8f011190b329b971a09a8fae9d157e01dcae8379468514"
dependencies = [
 "displaydoc",
 "icu_locale_core",
 "writeable",
 "yoke",
 "zerofrom",
 "zerotrie",
 "zerovec",
]

[[package]]
name = "idna"
version = "1.1.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3b0875f23caa03898994f6ddc501886a45c7d3d62d04d2d90788d47be1b1e4de"
dependencies = [
 "idna_adapter",
 "smallvec",
 "utf8_iter",
]

[[package]]
name = "idna_adapter"
version = "1.2.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cb68373c0d6620ef8105e855e7745e18b0d00d3bdb07fb532e434244cdb9a714"
dependencies = [
 "icu_normalizer",
 "icu_properties",
]

[[package]]
name = "indexmap"
version = "2.14.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cc4e190f5d26ca7051642629da2c52fc03bde85a03197c99408dcd291734c855"
dependencies = [
 "equivalent",
 "hashbrown 0.17.1",
]

[[package]]
name = "ipnet"
version = "2.12.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "791930b43c0d5973160d90a8f3894509f2b273430f5c5c73b668636d0287c5c0"

[[package]]
name = "is_terminal_polyfill"
version = "1.70.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a6cb138bb79a146c1bd460005623e142ef0181e3d0219cb493e02f7d08a35695"

[[package]]
name = "itoa"
version = "1.0.19"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3bd1574e617a9a6c1f8f8181bd7e841e1f20e4277607ef28945765188d6284d4"

[[package]]
name = "jobserver"
version = "0.1.35"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1c00acbd29eabad4a2392fa0e921c874934dbbf4194312ad20f04a0ed67a3cb3"
dependencies = [
 "getrandom 0.4.3",
 "libc",
]

[[package]]
name = "js-sys"
version = "0.3.107"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8dc09d2154098bbf4ef54eab6b117a3c24cbbffe94046b16ee6d6d4510584d3a"
dependencies = [
 "cfg-if",
 "futures-util",
 "wasm-bindgen",
]

[[package]]
name = "kvm-bindings"
version = "0.11.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3b13baf7bdfda2e10bcb109fcb099ef40cff82374eb6b7cdcf4695bdec4e522c"
dependencies = [
 "vmm-sys-util",
]

[[package]]
name = "kvm-ioctls"
version = "0.21.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "083c460d5a272c2f22205973e319147b791d92a288d7d7a8d4c6194f95229440"
dependencies = [
 "bitflags 2.13.2",
 "kvm-bindings",
 "libc",
 "vmm-sys-util",
]

[[package]]
name = "lazy_static"
version = "1.5.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "20870f649af7073d53e38067b2a84312175d56ea15217e1b15bc83506ec50afb"

[[package]]
name = "libc"
version = "0.2.191"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "65cd87c9a6d60536580b316ad985726104730a5151c99845f2490428ef91240a"

[[package]]
name = "libsqlite3-sys"
version = "0.30.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2e99fb7a497b1e3339bc746195567ed8d3e24945ecd636e3619d20b9de9e9149"
dependencies = [
 "cc",
 "pkg-config",
 "vcpkg",
]

[[package]]
name = "linux-raw-sys"
version = "0.12.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "32a66949e030da00e8c7d4434b251670a91556f4144941d37452769c25d58a53"

[[package]]
name = "litemap"
version = "0.8.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "47d9d19d1d6efa0109d2f65ff4c85cddd50bd572e5a00127ab10987290bcefae"

[[package]]
name = "lock_api"
version = "0.4.14"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "224399e74b87b5f3557511d98dff8b14089b3dadafcab6bb93eab67d3aace965"
dependencies = [
 "scopeguard",
]

[[package]]
name = "log"
version = "0.4.35"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5d15f86d12da44cd8d4ec3c73984537eb7f4cadcb9776a575a0a8a05ca11005c"

[[package]]
name = "lru-slab"
version = "0.1.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4050469837a6ff301cd14c1f8f24f88549e6d548f24f64e2148eb0f72cebc51f"

[[package]]
name = "matchers"
version = "0.2.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d1525a2a28c7f4fa0fc98bb91ae755d1e2d1505079e05539e35bc876b5d65ae9"
dependencies = [
 "regex-automata",
]

[[package]]
name = "matchit"
version = "0.7.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0e7465ac9959cc2b1404e8e2367b43684a6d13790fe23056cc8c6c5a6b7bcb94"

[[package]]
name = "memchr"
version = "2.8.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cf8baf1c55e62ffcace7a9f06f4bd9cd3f0c4beb022d3b367256b91b87513d98"

[[package]]
name = "mime"
version = "0.3.18"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d5848f5a9e8117cf94f2f90faac24a87ff81600ff8628c1e3deaab572e76bfa1"

[[package]]
name = "miniz_oxide"
version = "0.9.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b63fbc4a50860e98e7b2aa7804ded1db5cbc3aff9193adaff57a6931bf7c4b4c"
dependencies = [
 "adler2",
 "simd-adler32",
]

[[package]]
name = "mio"
version = "1.2.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1788edb87fdc09c7e26304471e2f5be8cdefb1b6930d6e3985fc02ff53bf86ee"
dependencies = [
 "libc",
 "wasi",
 "windows-sys 0.61.2",
]

[[package]]
name = "nix"
version = "0.29.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "71e2746dc3a24dd78b3cfcb7be93368c6de9963d30f43a6a73998a9cf4b17b46"
dependencies = [
 "bitflags 2.13.2",
 "cfg-if",
 "cfg_aliases",
 "libc",
]

[[package]]
name = "nu-ansi-term"
version = "0.50.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7957b9740744892f114936ab4a57b3f487491bbeafaf8083688b16841a4240e5"
dependencies = [
 "windows-sys 0.61.2",
]

[[package]]
name = "num-conv"
version = "0.2.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "521739c6d2bac4aa25192232afe6841231376b2b26d4d9fae5ecf8ca5772e441"

[[package]]
name = "once_cell"
version = "1.21.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9f7c3e4beb33f85d45ae3e3a1792185706c8e16d043238c593331cc7cd313b50"

[[package]]
name = "once_cell_polyfill"
version = "1.70.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "384b8ab6d37215f3c5301a95a4accb5d64aa607f1fcb26a11b5303878451b4fe"

[[package]]
name = "parking_lot"
version = "0.12.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "93857453250e3077bd71ff98b6a65ea6621a19bb0f559a85248955ac12c45a1a"
dependencies = [
 "lock_api",
 "parking_lot_core",
]

[[package]]
name = "parking_lot_core"
version = "0.9.12"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2621685985a2ebf1c516881c026032ac7deafcda1a2c9b7850dc81e3dfcb64c1"
dependencies = [
 "cfg-if",
 "libc",
 "redox_syscall",
 "smallvec",
 "windows-link",
]

[[package]]
name = "pem"
version = "3.0.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1d30c53c26bc5b31a98cd02d20f25a7c8567146caf63ed593a9d87b2775291be"
dependencies = [
 "base64 0.22.1",
 "serde_core",
]

[[package]]
name = "percent-encoding"
version = "2.3.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9b4f627cb1b25917193a259e49bdad08f671f8d9708acfd5fe0a8c1455d87220"

[[package]]
name = "pin-project-lite"
version = "0.2.17"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a89322df9ebe1c1578d689c92318e070967d1042b512afbe49518723f4e6d5cd"

[[package]]
name = "pkg-config"
version = "0.3.34"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f6b464fbc74e149a392436b17d523f769e057cb6877f6a5c4618bc6f11800548"

[[package]]
name = "potential_utf"
version = "0.1.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d83eb9bc6d8e5cf568e7a1101d60ee05e81ed50ea106026f3d18deeb046d7661"
dependencies = [
 "zerovec",
]

[[package]]
name = "powerfmt"
version = "0.2.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4a6394b9e965e73d0a289ee54f589087e2c676aedf60885baf52c76b771e4958"

[[package]]
name = "ppv-lite86"
version = "0.2.21"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "85eae3c4ed2f50dcfe72643da4befc30deadb458a9b590d720cde2f2b1e97da9"
dependencies = [
 "zerocopy",
]

[[package]]
name = "proc-macro2"
version = "1.0.107"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "985e7ec9bb745e6ce6535b544d84d6cd6f7ad8bd711c398938ae983b91a766d9"
dependencies = [
 "unicode-ident",
]

[[package]]
name = "quinn"
version = "0.11.12"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4051e23e9185c255a7e33ef59cdbca87a22d359052eecd22fc6b901fb37d9d11"
dependencies = [
 "bytes",
 "cfg_aliases",
 "pin-project-lite",
 "quinn-proto",
 "quinn-udp",
 "rustc-hash",
 "rustls",
 "socket2",
 "thiserror",
 "tokio",
 "tracing",
 "web-time",
]

[[package]]
name = "quinn-proto"
version = "0.11.19"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0e750cca55fe4f0439a15d0bb529da9651e79993e8e72c61a899a36d462befbe"
dependencies = [
 "bytes",
 "getrandom 0.4.3",
 "lru-slab",
 "rand 0.10.3",
 "rand_pcg",
 "ring",
 "rustc-hash",
 "rustls",
 "rustls-pki-types",
 "slab",
 "thiserror",
 "tinyvec",
 "tracing",
 "web-time",
]

[[package]]
name = "quinn-udp"
version = "0.5.16"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "af66907df18639dcf4db56ca65490cabc4b27a97dbadd96f2926cca73298f016"
dependencies = [
 "cfg_aliases",
 "libc",
 "once_cell",
 "socket2",
 "tracing",
 "windows-sys 0.61.2",
]

[[package]]
name = "quote"
version = "1.0.47"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1fbf4db142a473a8d80c26bbf18454ed458bf8d26c8219c331daecfdbd079001"
dependencies = [
 "proc-macro2",
]

[[package]]
name = "r-efi"
version = "6.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f8dcc9c7d52a811697d2151c701e0d08956f92b0e24136cf4cf27b57a6a0d9bf"

[[package]]
name = "rand"
version = "0.8.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e058c7de0b26af77780c769414d6257830bb240f3c38477dbc2c16e5f54d6d4c"
dependencies = [
 "libc",
 "rand_chacha",
 "rand_core 0.6.4",
]

[[package]]
name = "rand"
version = "0.10.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "65c9fb96cbc91e3478eaae79a69fcd3f1ae4ad052e471fe6732fff548984b4af"
dependencies = [
 "chacha20",
 "getrandom 0.4.3",
 "rand_core 0.10.1",
]

[[package]]
name = "rand_chacha"
version = "0.3.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e6c10a63a0fa32252be49d21e7709d4d4baf8d231c2dbce1eaa8141b9b127d88"
dependencies = [
 "ppv-lite86",
 "rand_core 0.6.4",
]

[[package]]
name = "rand_core"
version = "0.6.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ec0be4795e2f6a28069bec0b5ff3e2ac9bafc99e6a9a7dc3547996c5c816922c"
dependencies = [
 "getrandom 0.2.17",
]

[[package]]
name = "rand_core"
version = "0.10.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "63b8176103e19a2643978565ca18b50549f6101881c443590420e4dc998a3c69"

[[package]]
name = "rand_pcg"
version = "0.10.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "caa0f4137e1c0a72f4c651489402276c8e8e1cf081f3b0ba156d2cbeef09e86a"
dependencies = [
 "rand_core 0.10.1",
]

[[package]]
name = "rcgen"
version = "0.13.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "75e669e5202259b5314d1ea5397316ad400819437857b90861765f24c4cf80a2"
dependencies = [
 "pem",
 "ring",
 "rustls-pki-types",
 "time",
 "yasna",
]

[[package]]
name = "redox_syscall"
version = "0.5.18"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ed2bf2547551a7053d6fdfafda3f938979645c44812fbfcda098faae3f1a362d"
dependencies = [
 "bitflags 2.13.2",
]

[[package]]
name = "regex-automata"
version = "0.4.18"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ad8553b9b26413251cbf30e620595c7a41b3887f03da04579c0e6b0d6a06b4b2"
dependencies = [
 "aho-corasick",
 "memchr",
 "regex-syntax",
]

[[package]]
name = "regex-syntax"
version = "0.8.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d6f6ff9a378485b298a5286656da665ba74413d36db0979633275d2e708145d4"

[[package]]
name = "reqwest"
version = "0.12.28"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "eddd3ca559203180a307f12d114c268abf583f59b03cb906fd0b3ff8646c1147"
dependencies = [
 "base64 0.22.1",
 "bytes",
 "futures-core",
 "http",
 "http-body",
 "http-body-util",
 "hyper",
 "hyper-rustls",
 "hyper-util",
 "js-sys",
 "log",
 "percent-encoding",
 "pin-project-lite",
 "quinn",
 "rustls",
 "rustls-pki-types",
 "serde",
 "serde_json",
 "serde_urlencoded",
 "sync_wrapper",
 "tokio",
 "tokio-rustls",
 "tower",
 "tower-http",
 "tower-service",
 "url",
 "wasm-bindgen",
 "wasm-bindgen-futures",
 "web-sys",
 "webpki-roots 1.0.12",
]

[[package]]
name = "ring"
version = "0.17.14"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a4689e6c2294d81e88dc6261c768b63bc4fcdb852be6d1352498b114f61383b7"
dependencies = [
 "cc",
 "cfg-if",
 "getrandom 0.2.17",
 "libc",
 "untrusted",
 "windows-sys 0.52.0",
]

[[package]]
name = "rusqlite"
version = "0.32.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7753b721174eb8ff87a9a0e799e2d7bc3749323e773db92e0984debb00019d6e"
dependencies = [
 "bitflags 2.13.2",
 "fallible-iterator",
 "fallible-streaming-iterator",
 "hashlink",
 "libsqlite3-sys",
 "smallvec",
]

[[package]]
name = "rustc-hash"
version = "2.1.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6b1e7f9a428571be2dc5bc0505c13fb6bf936822b894ec87abf8a08a4e51742d"

[[package]]
name = "rustix"
version = "1.1.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e5e76742e5294153d4dde1207dbec5bdd81eef6fabdc0d2118babadabfd858aa"
dependencies = [
 "bitflags 2.13.2",
 "errno",
 "libc",
 "linux-raw-sys",
 "windows-sys 0.61.2",
]

[[package]]
name = "rustls"
version = "0.23.46"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "48e13bd8c0e9365c43cfa5c9e8f9ad49d3c8444926c9aac819e0e4dc503c8fdf"
dependencies = [
 "aws-lc-rs",
 "log",
 "once_cell",
 "ring",
 "rustls-pki-types",
 "rustls-webpki",
 "subtle",
 "zeroize",
]

[[package]]
name = "rustls-pki-types"
version = "1.16.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "102f35f7de7b06d8c61f1cff6592f5253e0dea431c74867aad5dfd70f44663fb"
dependencies = [
 "web-time",
 "zeroize",
]

[[package]]
name = "rustls-webpki"
version = "0.103.16"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4443b1b6d39d7c663139942cc4558262562d2c84123bb6282ef177009ca8b281"
dependencies = [
 "aws-lc-rs",
 "ring",
 "rustls-pki-types",
 "untrusted",
]

[[package]]
name = "rustversion"
version = "1.0.23"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cf54715a573b99ac80df0bc206da022bcd442c974952c7b9720069370852e21f"

[[package]]
name = "ryu"
version = "1.0.23"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9774ba4a74de5f7b1c1451ed6cd5285a32eddb5cccb8cc655a4e50009e06477f"

[[package]]
name = "scopeguard"
version = "1.2.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "94143f37725109f92c262ed2cf5e59bce7498c01bcc1502d7b9afe439a4e9f49"

[[package]]
name = "serde"
version = "1.0.229"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4148590afebada386688f18773da617792bf2ef03ffc1e4cbd2b1d45b023e0ba"
dependencies = [
 "serde_core",
 "serde_derive",
]

[[package]]
name = "serde_core"
version = "1.0.229"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "67dca2c9c51e58a4791a4b1ed58308b39c64224d349a935ab5039aa360942a48"
dependencies = [
 "serde_derive",
]

[[package]]
name = "serde_derive"
version = "1.0.229"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e7a5d71263a5a7d47b41f6b3f06ba276f10cc18b0931f1799f710578e2309348"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "serde_json"
version = "1.0.154"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e7e9cc8b1b85264074fbcc02a88680c4096b1e47df8f739dceb03bf482f04bd6"
dependencies = [
 "itoa",
 "memchr",
 "serde",
 "serde_core",
 "zmij",
]

[[package]]
name = "serde_path_to_error"
version = "0.1.20"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "10a9ff822e371bb5403e391ecd83e182e0e77ba7f6fe0160b795797109d1b457"
dependencies = [
 "itoa",
 "serde",
 "serde_core",
]

[[package]]
name = "serde_spanned"
version = "0.6.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bf41e0cfaf7226dca15e8197172c295a782857fcb97fad1808a166870dee75a3"
dependencies = [
 "serde",
]

[[package]]
name = "serde_urlencoded"
version = "0.7.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d3491c14715ca2294c4d6a88f15e84739788c1d030eed8c110436aafdaa2f3fd"
dependencies = [
 "form_urlencoded",
 "itoa",
 "ryu",
 "serde",
]

[[package]]
name = "sha2"
version = "0.10.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a7507d819769d01a365ab707794a4084392c824f54a7a6a7862f8c3d0892b283"
dependencies = [
 "cfg-if",
 "cpufeatures 0.2.17",
 "digest",
]

[[package]]
name = "sharded-slab"
version = "0.1.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f40ca3c46823713e0d4209592e8d6e826aa57e928f09752619fc696c499637f6"
dependencies = [
 "lazy_static",
]

[[package]]
name = "shlex"
version = "2.0.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f8fadd59c855ef2080decdef8ff161eb6661b86933c9d82e5ba29dc602a55aba"

[[package]]
name = "signal-hook-registry"
version = "1.4.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c4db69cba1110affc0e9f7bcd48bbf87b3f4fc7c61fc9155afd4c469eb3d6c1b"
dependencies = [
 "errno",
 "libc",
]

[[package]]
name = "simd-adler32"
version = "0.3.10"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3a219298ac11a56ea9a6d2120044824d6f01aeb034955e7af7bc16858527deea"

[[package]]
name = "slab"
version = "0.4.12"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0c790de23124f9ab44544d7ac05d60440adc586479ce501c1d6d7da3cd8c9cf5"

[[package]]
name = "smallvec"
version = "1.16.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "149309498a5f7334b12ddae0cad8aba81ce785fe91755068867e017cf98fee46"

[[package]]
name = "socket2"
version = "0.6.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c3d1e2c7f27f8d4cb10542a02c49005dbd6e93095799d6f3be745fae9f8fedd4"
dependencies = [
 "libc",
 "windows-sys 0.61.2",
]

[[package]]
name = "stable_deref_trait"
version = "1.2.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6ce2be8dc25455e1f91df71bfa12ad37d7af1092ae736f3a6cd0e37bc7810596"

[[package]]
name = "strsim"
version = "0.11.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7da8b5736845d9f2fcb837ea5d9e2628564b3b043a70948a3f0b778838c5fb4f"

[[package]]
name = "subtle"
version = "2.6.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "13c2bddecc57b384dee18652358fb23172facb8a2c51ccc10d74c157bdea3292"

[[package]]
name = "syn"
version = "2.0.119"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "872831b642d1a07999a962a351ed35b955ea2cfc8f3862091e2a240a84f17297"
dependencies = [
 "proc-macro2",
 "quote",
 "unicode-ident",
]

[[package]]
name = "syn"
version = "3.0.10"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4ae9ccbea3a7b0b09443036490c8f34a8b1b760fd345e5c0dae0aac718faea79"
dependencies = [
 "proc-macro2",
 "quote",
 "unicode-ident",
]

[[package]]
name = "sync_wrapper"
version = "1.0.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0bf256ce5efdfa370213c1dabab5935a12e49f2c58d15e9eac2870d3b4f27263"
dependencies = [
 "futures-core",
]

[[package]]
name = "synstructure"
version = "0.14.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "901704edd0dfe137f1987838ee4f259e4e063c31371bdb423f7ae38ec6f77f02"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "tar"
version = "0.4.46"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3f6221d9a6003c78398e3b239969f352578258df48c8eb051caadae0015bc840"
dependencies = [
 "filetime",
 "libc",
 "xattr",
]

[[package]]
name = "tempfile"
version = "3.27.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "32497e9a4c7b38532efcdebeef879707aa9f794296a4f0244f6f69e9bc8574bd"
dependencies = [
 "fastrand",
 "getrandom 0.4.3",
 "once_cell",
 "rustix",
 "windows-sys 0.61.2",
]

[[package]]
name = "thiserror"
version = "2.0.21"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "09e52cb86a36cede5cb101bf8908837b3e4c6e5e59fe7fd85c23fb56200d189e"
dependencies = [
 "thiserror-impl",
]

[[package]]
name = "thiserror-impl"
version = "2.0.21"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "fe5197923287db20a58125f0bc85c062f7f2c892de97b18c356f9efb14b28524"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "thread_local"
version = "1.1.10"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1ad99c4c6d32803332c548b1af0540b357b3f5fc0be8f6c6bfe8b2e6ae784070"
dependencies = [
 "cfg-if",
]

[[package]]
name = "time"
version = "0.3.55"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cdb87b95ec50ddfa440816d227a17b2ccbdda963a316a727fda0fc4334f7d134"
dependencies = [
 "deranged",
 "num-conv",
 "powerfmt",
 "serde_core",
 "time-core",
 "time-macros",
]

[[package]]
name = "time-core"
version = "0.1.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9e1c906769ad99c88eaa54e728060edef082f8e358ff32030cb7c7d315e81109"

[[package]]
name = "time-macros"
version = "0.2.32"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7e689342a48d2ea927c87ea50cabf8594854bf940e9310208848d680d668ed85"
dependencies = [
 "num-conv",
 "time-core",
]

[[package]]
name = "tinystr"
version = "0.8.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "abc840c238154d58f9e86b2792261edfde565a17696e1cda1a449de27979801a"
dependencies = [
 "displaydoc",
 "zerovec",
]

[[package]]
name = "tinyvec"
version = "1.13.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "fd3ca314f692efd6c868f8408f53fe444634a845f96c028b97d35f6a1f79f0ee"

[[package]]
name = "tokio"
version = "1.54.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b52fd3255258d26fac7264ab5a76b53c13367136dc011d3d6ad4f8ed070396a3"
dependencies = [
 "bytes",
 "libc",
 "mio",
 "parking_lot",
 "pin-project-lite",
 "signal-hook-registry",
 "socket2",
 "tokio-macros",
 "windows-sys 0.61.2",
]

[[package]]
name = "tokio-macros"
version = "2.7.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "78773a2a397f451582ce068015985c33193cf6dea8b74d2a639fe457b2f07b0e"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "tokio-rustls"
version = "0.26.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c9cc2678c2cdd569ef8215e2afd7954ada2ae20b4fdd2c5fe6139a3b02d105db"
dependencies = [
 "rustls",
 "tokio",
]

[[package]]
name = "tokio-util"
version = "0.7.20"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e464cf451ba96ebfc6f9b6542f17ee8b8956e33f1e40d9690624e59d7a7f8a4b"
dependencies = [
 "bytes",
 "futures-core",
 "futures-sink",
 "libc",
 "pin-project-lite",
 "tokio",
]

[[package]]
name = "toml"
version = "0.8.23"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "dc1beb996b9d83529a9e75c17a1686767d148d70663143c7854d8b4a09ced362"
dependencies = [
 "serde",
 "serde_spanned",
 "toml_datetime",
 "toml_edit",
]

[[package]]
name = "toml_datetime"
version = "0.6.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "22cddaf88f4fbc13c51aebbf5f8eceb5c7c5a9da2ac40a13519eb5b0a0e8f11c"
dependencies = [
 "serde",
]

[[package]]
name = "toml_edit"
version = "0.22.27"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "41fe8c660ae4257887cf66394862d21dbca4a6ddd26f04a3560410406a2f819a"
dependencies = [
 "indexmap",
 "serde",
 "serde_spanned",
 "toml_datetime",
 "toml_write",
 "winnow",
]

[[package]]
name = "toml_write"
version = "0.1.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5d99f8c9a7727884afe522e9bd5edbfc91a3312b36a77b5fb8926e4c31a41801"

[[package]]
name = "tower"
version = "0.5.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ebe5ef63511595f1344e2d5cfa636d973292adc0eec1f0ad45fae9f0851ab1d4"
dependencies = [
 "futures-core",
 "futures-util",
 "pin-project-lite",
 "sync_wrapper",
 "tokio",
 "tower-layer",
 "tower-service",
 "tracing",
]

[[package]]
name = "tower-http"
version = "0.6.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4cfcf7e2740e6fc6d4d688b4ef00650406bb94adf4731e43c096c3a19fe40840"
dependencies = [
 "bitflags 2.13.2",
 "bytes",
 "futures-util",
 "http",
 "http-body",
 "pin-project-lite",
 "tower",
 "tower-layer",
 "tower-service",
 "url",
]

[[package]]
name = "tower-layer"
version = "0.3.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "121c2a6cda46980bb0fcd1647ffaf6cd3fc79a013de288782836f6df9c48780e"

[[package]]
name = "tower-service"
version = "0.3.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8df9b6e13f2d32c91b9bd719c00d1958837bc7dec474d94952798cc8e69eeec3"

[[package]]
name = "tracing"
version = "0.1.44"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "63e71662fa4b2a2c3a26f570f037eb95bb1f85397f3cd8076caed2f026a6d100"
dependencies = [
 "log",
 "pin-project-lite",
 "tracing-attributes",
 "tracing-core",
]

[[package]]
name = "tracing-attributes"
version = "0.1.31"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7490cfa5ec963746568740651ac6781f701c9c5ea257c58e057f3ba8cf69e8da"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 2.0.119",
]

[[package]]
name = "tracing-core"
version = "0.1.36"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "db97caf9d906fbde555dd62fa95ddba9eecfd14cb388e4f491a66d74cd5fb79a"
dependencies = [
 "once_cell",
 "valuable",
]

[[package]]
name = "tracing-log"
version = "0.2.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ee855f1f400bd0e5c02d150ae5de3840039a3f54b025156404e34c23c03f47c3"
dependencies = [
 "log",
 "once_cell",
 "tracing-core",
]

[[package]]
name = "tracing-subscriber"
version = "0.3.23"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cb7f578e5945fb242538965c2d0b04418d38ec25c79d160cd279bf0731c8d319"
dependencies = [
 "matchers",
 "nu-ansi-term",
 "once_cell",
 "regex-automata",
 "sharded-slab",
 "smallvec",
 "thread_local",
 "tracing",
 "tracing-core",
 "tracing-log",
]

[[package]]
name = "try-lock"
version = "0.2.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e421abadd41a4225275504ea4d6566923418b7f05506fbc9c0fe86ba7396114b"

[[package]]
name = "typenum"
version = "1.20.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b6f5e870be6c3b371b77fe0ee0bafb859fa4964b4404c27de1d380043c4dda20"

[[package]]
name = "unicode-ident"
version = "1.0.27"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a2c754d6c33795a1c324727428e5a7dedb5b06195f9890bdbcba760d3e246563"

[[package]]
name = "untrusted"
version = "0.9.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8ecb6da28b8a351d773b68d5825ac39017e680750f980f3a1a85cd8dd28a47c1"

[[package]]
name = "ureq"
version = "2.12.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "02d1a66277ed75f640d608235660df48c8e3c19f3b4edb6a263315626cc3c01d"
dependencies = [
 "base64 0.22.1",
 "flate2",
 "log",
 "once_cell",
 "rustls",
 "rustls-pki-types",
 "url",
 "webpki-roots 0.26.11",
]

[[package]]
name = "url"
version = "2.5.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ff67a8a4397373c3ef660812acab3268222035010ab8680ec4215f38ba3d0eed"
dependencies = [
 "form_urlencoded",
 "idna",
 "percent-encoding",
 "serde",
]

[[package]]
name = "utf8_iter"
version = "1.1.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "af3bd9d3e571c26fcce9d033b87ea5ab061b8d2ffe9b7a5675d810d68f03edf0"
dependencies = [
 "utf_types",
]

[[package]]
name = "utf8parse"
version = "0.2.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "06abde3611657adf66d383f00b093d7faecc7fa57071cce2578660c9f1010821"

[[package]]
name = "utf_types"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3a3b425286980a367278453f3b6d28cdcb134b574e5a35824a6ff5eebad50bb6"

[[package]]
name = "uuid"
version = "1.29.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "bed99139f7dac9b5cfe5f17be4d66dc7079a34dee1d74bcc77217119240e267c"
dependencies = [
 "getrandom 0.4.3",
 "js-sys",
 "wasm-bindgen",
]

[[package]]
name = "v0_4-kvm-uffd-wp-poc"
version = "0.5.3"
dependencies = [
 "anyhow",
 "kvm-bindings",
 "kvm-ioctls",
 "libc",
 "nix",
 "parking_lot",
]

[[package]]
name = "v0_4-restore-poc"
version = "0.5.3"
dependencies = [
 "anyhow",
 "forkd-uffd",
 "kvm-bindings",
 "kvm-ioctls",
 "libc",
]

[[package]]
name = "v0_4-thp-uffd-wp-poc"
version = "0.5.3"
dependencies = [
 "anyhow",
 "libc",
 "nix",
]

[[package]]
name = "v0_4-uffd-wp-poc"
version = "0.5.3"
dependencies = [
 "anyhow",
 "libc",
 "nix",
 "parking_lot",
 "rand 0.8.8",
]

[[package]]
name = "valuable"
version = "0.1.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ba73ea9cf16a25df0c8caa16c51acb937d5712a8429db78a3ee29d5dcacd3a65"

[[package]]
name = "vcpkg"
version = "0.2.15"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "accd4ea62f7bb7a82fe23066fb0957d48ef677f6eeb8215f372f52e48bb32426"

[[package]]
name = "version_check"
version = "0.9.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0b928f33d975fc6ad9f86c8f283853ad26bdd5b10b7f1542aa2fa15e2289105a"

[[package]]
name = "vmm-sys-util"
version = "0.12.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1d1435039746e20da4f8d507a72ee1b916f7b4b05af7a91c093d2c6561934ede"
dependencies = [
 "bitflags 1.3.2",
 "libc",
]

[[package]]
name = "want"
version = "0.3.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ec4cdd0dd910afe868b7ef477227d8d538b46b3075031afee8a9f2acb0a2ed0b"
dependencies = [
 "try-lock",
]

[[package]]
name = "wasi"
version = "0.11.1+wasi-snapshot-preview1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ccf3ec651a847eb01de73ccad15eb7d99f80485de043efb2f370cd654f4ea44b"

[[package]]
name = "wasm-bindgen"
version = "0.2.130"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "a0181c14ddd6c7b8b0b0132a5f1ea2f32be0d893cd3ea97b6e3b28886b8a3d1c"
dependencies = [
 "cfg-if",
 "rustversion",
 "wasm-bindgen-macro",
 "wasm-bindgen-shared",
]

[[package]]
name = "wasm-bindgen-futures"
version = "0.4.80"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5e14e0327cb5368ad60ccb22d04a9827a693c95a926d8d125e9fa39e0baec501"
dependencies = [
 "js-sys",
 "tokio",
 "wasm-bindgen",
]

[[package]]
name = "wasm-bindgen-macro"
version = "0.2.130"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "6e931a02a39abdbf66966fc35902b5ed58344750a70885bed4ebb3d8eaf28ed9"
dependencies = [
 "quote",
 "wasm-bindgen-macro-support",
]

[[package]]
name = "wasm-bindgen-macro-support"
version = "0.2.130"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "2ca4b54280e455bd831e33a6b8e734baf432fe63fbc35c42ac14ce67a043a53a"
dependencies = [
 "bumpalo",
 "proc-macro2",
 "quote",
 "syn 3.0.10",
 "wasm-bindgen-shared",
]

[[package]]
name = "wasm-bindgen-shared"
version = "0.2.130"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3b1f9c3798725016c6ee5f2f57869ec0831e8bbc0ab0310a86e787e97f41389e"
dependencies = [
 "unicode-ident",
]

[[package]]
name = "web-sys"
version = "0.3.107"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "d7d179596ff5ec0f6de4db64d54011bde6289cc49603d3a6e4ce3f329207ab88"
dependencies = [
 "js-sys",
 "wasm-bindgen",
]

[[package]]
name = "web-time"
version = "1.1.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "5a6580f308b1fad9207618087a65c04e7a10bc77e02c8e84e9b00dd4b12fa0bb"
dependencies = [
 "js-sys",
 "wasm-bindgen",
]

[[package]]
name = "webpki-roots"
version = "0.26.11"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "521bc38abb08001b01866da9f51eb7c5d647a19260e00054a8c7fd5f9e57f7a9"
dependencies = [
 "webpki-roots 1.0.12",
]

[[package]]
name = "webpki-roots"
version = "1.0.12"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "7265c9301c16a8711c992c2d8da90ee77ef54b1ed51482d6dfeb00dc756b1297"
dependencies = [
 "rustls-pki-types",
]

[[package]]
name = "windows-link"
version = "0.2.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f0805222e57f7521d6a62e36fa9163bc891acd422f971defe97d64e70d0a4fe5"

[[package]]
name = "windows-sys"
version = "0.52.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "282be5f36a8ce781fad8c8ae18fa3f9beff57ec1b52cb3de0789201425d9a33d"
dependencies = [
 "windows-targets",
]

[[package]]
name = "windows-sys"
version = "0.61.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ae137229bcbd6cdf0f7b80a31df61766145077ddf49416a728b02cb3921ff3fc"
dependencies = [
 "windows-link",
]

[[package]]
name = "windows-targets"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "9b724f72796e036ab90c1021d4780d4d3d648aca59e491e6b98e725b84e99973"
dependencies = [
 "windows_aarch64_gnullvm",
 "windows_aarch64_msvc",
 "windows_i686_gnu",
 "windows_i686_gnullvm",
 "windows_i686_msvc",
 "windows_x86_64_gnu",
 "windows_x86_64_gnullvm",
 "windows_x86_64_msvc",
]

[[package]]
name = "windows_aarch64_gnullvm"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "32a4622180e7a0ec044bb555404c800bc9fd9ec262ec147edd5989ccd0c02cd3"

[[package]]
name = "windows_aarch64_msvc"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "09ec2a7bb152e2252b53fa7803150007879548bc709c039df7627cabbd05d469"

[[package]]
name = "windows_i686_gnu"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "8e9b5ad5ab802e97eb8e295ac6720e509ee4c243f69d781394014ebfe8bbfa0b"

[[package]]
name = "windows_i686_gnullvm"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0eee52d38c090b3caa76c563b86c3a4bd71ef1a819287c19d586d7334ae8ed66"

[[package]]
name = "windows_i686_msvc"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "240948bc05c5e7c6dabba28bf89d89ffce3e303022809e73deaefe4f6ec56c66"

[[package]]
name = "windows_x86_64_gnu"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "147a5c80aabfbf0c7d901cb5895d1de30ef2907eb21fbbab29ca94c5b08b1a78"

[[package]]
name = "windows_x86_64_gnullvm"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "24d5b23dc417412679681396f2b49f3de8c1473deb516bd34410872eff51ed0d"

[[package]]
name = "windows_x86_64_msvc"
version = "0.52.6"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "589f6da84c646204747d1270a2a5661ea66ed1cced2631d546fdfb155959f9ec"

[[package]]
name = "winnow"
version = "0.7.15"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "df79d97927682d2fd8adb29682d1140b343be4ac0f08fd68b7765d9c059d3945"
dependencies = [
 "memchr",
]

[[package]]
name = "writeable"
version = "0.6.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "3ad82d2a33cdc9674dc7465672f271e096168fcdbe0f799d9e6db8c5892679dc"

[[package]]
name = "xattr"
version = "1.7.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "963d6fab92d5504b971dc8bd90ca743c2575e590bc8071ea5bc571598667ae08"
dependencies = [
 "libc",
 "rustix",
]

[[package]]
name = "yasna"
version = "0.5.2"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e17bb3549cc1321ae1296b9cdc2698e2b6cb1992adfa19a8c72e5b7a738f44cd"
dependencies = [
 "time",
]

[[package]]
name = "yoke"
version = "0.8.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "417ef3fcf6c300b7dbd25c85531a746931c02ad536f3cf4786ed1e88223340b3"
dependencies = [
 "stable_deref_trait",
 "yoke-derive",
 "zerofrom",
]

[[package]]
name = "yoke-derive"
version = "0.8.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "ec8ebde2db3681e8c9980cc27822030e68752690ddfa9473e739aeb4dbde6d71"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
 "synstructure",
]

[[package]]
name = "zerocopy"
version = "0.8.63"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e5fe1f8f1b06191a00962174c61aa5005e0bb391a6d80d07e24d115c01a92ed8"
dependencies = [
 "zerocopy-derive",
]

[[package]]
name = "zerocopy-derive"
version = "0.8.63"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "863ad3ac83293fb4d740aedbfdc9240dd8d1a50c1099acd76ce80ce7c7230c7f"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 2.0.119",
]

[[package]]
name = "zerofrom"
version = "0.1.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "0ec05a11813ea801ff6d75110ad09cd0824ddba17dfe17128ea0d5f68e6c5272"
dependencies = [
 "zerofrom-derive",
]

[[package]]
name = "zerofrom-derive"
version = "0.1.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f75b4683f6c7f45248d4d64056a24298c6281e0993356d7d1b4a1a962ef10d4a"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
 "synstructure",
]

[[package]]
name = "zeroize"
version = "1.9.1"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "e13084392c5e4bc371903e2935a5eaeed24905a7511356b883835e18a78f6879"

[[package]]
name = "zerotrie"
version = "0.2.5"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "4ea269c3bd32f0a32c321907a2ae912ba6f4649bb0fc764a15627e99a7095a3f"
dependencies = [
 "displaydoc",
 "yoke",
 "zerofrom",
]

[[package]]
name = "zerovec"
version = "0.11.9"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "f58a2799cfe9a1670c7ea8edbd52ef8edb93cb37a7f83bf31ba9e35d9dae6a40"
dependencies = [
 "yoke",
 "zerofrom",
 "zerovec-derive",
]

[[package]]
name = "zerovec-derive"
version = "0.11.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "c3743ddbf47738d929cd1ae2e199d40d0162979dfe31aef8ae646d2bf00bf97a"
dependencies = [
 "proc-macro2",
 "quote",
 "syn 3.0.10",
]

[[package]]
name = "zlib-rs"
version = "0.6.8"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "b268e58e7c693d7c271f93ffc4ba3b380412554231c85bf61ca7af91042a4112"

[[package]]
name = "zmij"
version = "1.0.23"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "29666d0abbfad1e3dc4dcf6144730dd3a3ab225bbbdac83319345b1b44ccfc1b"

[[package]]
name = "zstd"
version = "0.13.4"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "cdb02c3132d251ab1606e9fc913a7da6c6ed08cc263df15edf082e1dc497d157"
dependencies = [
 "zstd-safe",
]

[[package]]
name = "zstd-safe"
version = "7.4.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "1c8ea04668f5de8d8d51051714ba560c9536519cadcdb8829bba4fe2ad159b03"
dependencies = [
 "zstd-sys",
]

[[package]]
name = "zstd-sys"
version = "2.1.2+zstd.1.5.7"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "481ada9c543b2323cbb7bbdbb5249177fbf0188ed959ed2c15e4b22421c84ed3"
dependencies = [
 "cc",
 "pkg-config",
]
"""
