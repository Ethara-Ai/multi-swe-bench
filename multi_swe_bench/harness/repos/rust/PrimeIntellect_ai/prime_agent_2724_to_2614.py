import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Era 2 of PrimeIntellect-ai/prime-agent: the Rust workspace (`rust` branch).
#   PR    base sha  branch  date        tree at base
#   2614  4ac52e22  rust    2026-09-23  Cargo workspace + committed Cargo.lock,
#                                       toolchain 1.98.1 in every CI workflow
#   2724  c934fdd5  rust    2026-09-24  same pins as 2614
# Both bases share toolchain, lockfile model, workspace layout and test
# framework (libtest; plus a stdlib-only unittest/pytest file under
# scripts/release for #2724). The TypeScript-era PR on `main` (#2160) is a
# separate era config: prime_agent_2160_to_2160.py.
_ORG = "PrimeIntellect-ai"
_INTERVAL = "prime_agent_2724_to_2614"

# Toolchain pin: every workflow at 4ac52e22 / c934fdd5 pins
# `toolchain: "1.98.1"` (.github/workflows/continuous.yml, release.yml, and
# ci.yml at c934fdd5). Python test files in this era (scripts/release/*) use
# only the standard library; bookworm's python3 is 3.11.
# Mirror of the Docker Hub official image (same digests, no pull rate limit).
_RUST_VERSION = "1.98.1"
_RUST_IMAGE = f"public.ecr.aws/docker/library/rust:{_RUST_VERSION}-bookworm"
_PYTEST_VERSION = "8.4.2"
# Native surface of Cargo.lock is ring/cc only: the core apt set suffices.
_EXTRA_APT = ""

# Cargo workspace manifest (relative to the repo root).
_MANIFEST = "Cargo.toml"
# Extra `cargo test` arguments and libtest arguments for every graded target.
_CARGO_TEST_ARGS = ""
_LIBTEST_ARGS = ""
_TARGET_TIMEOUT = 1800

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
# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Compiles every cargo test target this PR's test files map onto (or, for a
# test file the PR adds, every test target of its package) and collects the
# PR's Python tests, which also warms the build cache the graded runs reuse.
python3 /home/msb_tests.py gate
"""

_CARGO_PROVISION = """cd "$(dirname /home/@@REPO@@/@@MANIFEST@@)"
cargo fetch --locked
cd /home/@@REPO@@
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


class PrimeAgent2724To2614ImageBase(Image):
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


class PrimeAgent2724To2614ImageDefault(Image):
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
        return PrimeAgent2724To2614ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare(self) -> str:
        pr = self.pr
        return (
            _render(_PREPARE, pr)
            .replace("@@SHA@@", pr.base.sha)
            .replace("@@CARGO_PROVISION@@", _render(_CARGO_PROVISION, pr))
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
class PRIME_AGENT_2724_TO_2614(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PrimeAgent2724To2614ImageDefault(self.pr, self._config)

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
