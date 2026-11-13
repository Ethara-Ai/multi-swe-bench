import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# rust-toolchain.toml pins channel 1.95.0 (workspace rust-version 1.94, and
# mise.toml rust = "1.95.0") at every base sha in the dataset (#5048 9cc5f6e4,
# #5244 f4a61375), so a single era. protoc is vendored (protoc-bin-vendored),
# so no system protobuf-compiler is needed.
_RUST_IMAGE = "rust:1.95.0-bookworm"

_ENV = """export CI=true
export NO_COLOR=1
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS=4
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
export RUST_TEST_THREADS=1
export OPENSHELL_TELEMETRY_ENABLED=false
# rust-lld (default x86_64 linker since 1.90) segfaults under QEMU amd64 emulation; use the system linker
export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_RUSTFLAGS='-C linker-features=-lld'"""

# Disposable PostgreSQL that the repo's `mise run test:rust:postgres` would
# otherwise start in a Docker/Podman container. Here it is the distro server
# inside the test container, listening on loopback only.
_PG_URL = "postgres://openshell:openshell@127.0.0.1:5432/openshell"

_PG_START = """pg_ver=$(ls /usr/lib/postgresql | sort -V | tail -n1)
if ! pg_ctlcluster "$pg_ver" main status >/dev/null 2>&1; then
  pg_ctlcluster "$pg_ver" main start
fi
for _ in $(seq 1 60); do
  pg_isready -q -h 127.0.0.1 -p 5432 && break
  sleep 1
done
pg_isready -h 127.0.0.1 -p 5432"""

_SUITE_MARK = "openshell-suite"

# Per-target test runner, written to /home/openshell_tests.py. For a test
# target that does not compile while test.patch is applied it reports, as
# FAILED, only the #[test] fns that test.patch adds or modifies in that
# target's source files, named as libtest names them (module path derived from
# the crate root's `mod` tree). Nothing is reported for such a target when
# test.patch is not applied.
_RUNNER = r'''#!/usr/bin/env python3
"""Per-target cargo test runner.

Builds every test target of the selected packages with
`cargo build --tests --keep-going`, so one test target that does not compile
does not stop the others, then runs each built test binary the way
`cargo test` does (same "Running" header, cwd = package root, CARGO_* env).

For a test target that did not compile while /home/test.patch is applied,
the #[test] functions that test.patch adds or modifies in that target's
source files are reported as FAILED, under the names libtest would give
them (crate-relative module path + fn name). Untouched tests of such a
target are not reported.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

TEST_PATCH = "/home/test.patch"
LIB_KINDS = {"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"}


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- rust source


_CHAR_RE = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^\\'\n])'")
_RAW_RE = re.compile(r'r(#*)"')


def _ident(ch):
    return ch.isalnum() or ch == "_"


def blank(src):
    """Replace comments, strings and char literals with spaces (keep newlines)."""
    out = list(src)
    n = len(src)
    i = 0

    def wipe(a, b):
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            wipe(i, j)
            i = j
        elif c == "/" and src.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif src.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            wipe(i, j)
            i = j
        elif c == "r" and (
            i == 0
            or not _ident(src[i - 1])
            or (src[i - 1] == "b" and (i < 2 or not _ident(src[i - 2])))
        ) and _RAW_RE.match(src, i):
            hashes = _RAW_RE.match(src, i).group(1)
            start = i + 1 + len(hashes) + 1
            j = src.find('"' + hashes, start)
            j = n if j < 0 else j + 1 + len(hashes)
            wipe(i, j)
            i = j
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            wipe(i, j)
            i = j
        elif c == "'":
            m = _CHAR_RE.match(src, i)
            if m:
                wipe(i, m.end())
                i = m.end()
            else:
                i += 1
        else:
            i += 1
    return "".join(out)


def pairs(code, open_ch, close_ch):
    stack, fwd, back = [], {}, {}
    for i, c in enumerate(code):
        if c == open_ch:
            stack.append(i)
        elif c == close_ch and stack:
            o = stack.pop()
            fwd[o] = i
            back[i] = o
    return fwd, back


_QUAL_RE = re.compile(
    r"(?:\bpub\s*(?:\([^()]*\))?|\basync|\bunsafe|\bconst|\bextern|\bdefault)\s*$"
)


def attrs_before(src, code, pos, sq_back):
    """Outer attributes (text) directly preceding the item keyword at pos,
    and the offset where the first of them starts."""
    attrs = []
    start = pos
    p = pos
    while True:
        head = code[:p].rstrip()
        m = _QUAL_RE.search(head)
        if m and m.start() < len(head):
            p = m.start()
            start = p
            continue
        if head.endswith("]"):
            close = len(head) - 1
            o = sq_back.get(close)
            if o is not None:
                h = code[:o].rstrip()
                if h.endswith("#"):
                    a = len(h) - 1
                    attrs.append(src[a : close + 1])
                    p = a
                    start = a
                    continue
        break
    return list(reversed(attrs)), start


_FOREIGN_CFG = re.compile(r"#\[\s*cfg\s*\((?P<body>.*)\)\s*\]", re.S)


def foreign_only(attrs):
    """True when a cfg attribute restricts the item to a non-Linux target."""
    for a in attrs:
        m = _FOREIGN_CFG.match(a)
        if not m:
            continue
        body = m.group("body")
        if "not(" in body or "any(" in body:
            continue
        if re.search(r'\bwindows\b|target_os\s*=\s*"(?:windows|macos|ios|freebsd)"', body):
            return True
    return False


class RustFile:
    def __init__(self, path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            self.src = fh.read()
        self.code = blank(self.src)
        self.cur_fwd, _ = pairs(self.code, "{", "}")
        _, self.sq_back = pairs(self.code, "[", "]")
        self.line_starts = [0]
        for i, c in enumerate(self.src):
            if c == "\n":
                self.line_starts.append(i + 1)
        self.inline = []  # (name, open, close, foreign)
        self.decls = []  # (pos, name, attrs)
        for m in re.finditer(r"\bmod\s+(?:r#)?([A-Za-z_]\w*)\s*([{;])", self.code):
            attrs, _ = attrs_before(self.src, self.code, m.start(), self.sq_back)
            if m.group(2) == "{":
                o = m.end() - 1
                self.inline.append(
                    (m.group(1), o, self.cur_fwd.get(o, len(self.code)), foreign_only(attrs))
                )
            else:
                self.decls.append((m.start(), m.group(1), attrs))

    def line(self, pos):
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def enclosing(self, pos):
        mods = [m for m in self.inline if m[1] < pos < m[2]]
        mods.sort(key=lambda m: m[1])
        return [m[0] for m in mods], any(m[3] for m in mods)

    def tests(self):
        """(module path inside this file, fn name, first line, last line, ignored)"""
        found = []
        for m in re.finditer(r"\bfn\s+(?:r#)?([A-Za-z_]\w*)", self.code):
            attrs, start = attrs_before(self.src, self.code, m.start(), self.sq_back)
            if not any(re.match(r"#\[\s*(?:[A-Za-z_]\w*::)*test\s*[\](]", a) for a in attrs):
                continue
            if foreign_only(attrs):
                continue
            depth, j, body = 0, m.end(), None
            while j < len(self.code):
                c = self.code[j]
                if c in "([":
                    depth += 1
                elif c in ")]":
                    depth -= 1
                elif c == ";" and depth <= 0:
                    break
                elif c == "{" and depth <= 0:
                    body = j
                    break
                j += 1
            if body is None:
                continue
            end = self.cur_fwd.get(body, len(self.code) - 1)
            mods, foreign = self.enclosing(m.start())
            if foreign:
                continue
            ignored = any(re.match(r"#\[\s*ignore\b", a) for a in attrs)
            found.append((mods, m.group(1), self.line(start), self.line(end), ignored))
        return found


def module_tree(root):
    """[(file, crate-relative module path)] reachable from a crate root file."""
    result, seen = [], set()

    def walk(path, modpath, modrs):
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(path):
            return
        seen.add(real)
        rf = RustFile(path)
        result.append((real, modpath, rf))
        here = os.path.dirname(path)
        base = here if modrs else os.path.join(here, os.path.splitext(os.path.basename(path))[0])
        for pos, name, attrs in rf.decls:
            if foreign_only(attrs):
                continue
            chain, foreign = rf.enclosing(pos)
            if foreign:
                continue
            custom = None
            for a in attrs:
                pm = re.match(r'#\[\s*path\s*=\s*"([^"]+)"\s*\]', a)
                if pm:
                    custom = pm.group(1)
            if custom is not None:
                child = os.path.join(here if not chain else os.path.join(base, *chain), custom)
                candidates = [child]
            else:
                d = os.path.join(base, *chain)
                candidates = [os.path.join(d, name + ".rs"), os.path.join(d, name, "mod.rs")]
            for cand in candidates:
                if os.path.isfile(cand):
                    walk(cand, modpath + chain + [name], os.path.basename(cand) == "mod.rs")
                    break

    walk(root, [], True)
    return result


# ------------------------------------------------------------------- patches


def changed_lines(patch_text):
    """{repo-relative path: set(new-side line numbers touched)}"""
    touched = {}
    cur = None
    new_line = 0
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            cur = None
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else touched.setdefault(p[2:] if p.startswith("b/") else p, set())
            continue
        if line.startswith("--- "):
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            new_line = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            cur.add(new_line)
            new_line += 1
        elif line.startswith("-"):
            cur.add(new_line)
        elif line.startswith(" ") or line == "":
            new_line += 1
    return touched


def test_patch_applied(repo):
    if not os.path.isfile(TEST_PATCH) or os.path.getsize(TEST_PATCH) == 0:
        return False
    r = subprocess.run(
        ["git", "apply", "--check", "-R", "--whitespace=nowarn", TEST_PATCH],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


# ------------------------------------------------------------------ libtest


def libtest_filter(args):
    filters, skips, mode = [], [], "normal"
    exact = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--skip" and i + 1 < len(args):
            skips.append(args[i + 1])
            i += 2
            continue
        if a in ("--test-threads", "--format", "--color", "-Z", "--logfile", "--report-time"):
            i += 2 if a != "--report-time" else 1
            continue
        if a == "--ignored":
            mode = "ignored"
        elif a == "--include-ignored":
            mode = "all"
        elif a == "--exact":
            exact = True
        elif not a.startswith("-"):
            filters.append(a)
        i += 1

    def keep(name, ignored):
        if mode == "normal" and ignored:
            return False
        if mode == "ignored" and not ignored:
            return False
        if filters and not any((f == name) if exact else (f in name) for f in filters):
            return False
        if any((s == name) if exact else (s in name) for s in skips):
            return False
        return True

    return keep


# --------------------------------------------------------------------- main


def target_group(kinds):
    return "lib" if set(kinds) & LIB_KINDS else kinds[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cargo-args", required=True)
    ap.add_argument("--doc", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("libtest", nargs="*")
    opts = ap.parse_args()
    cargo_args = shlex.split(opts.cargo_args)
    repo = os.getcwd()

    meta = json.loads(
        subprocess.run(
            ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked", "--offline"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    )
    target_dir = meta["target_directory"]
    selected = []
    for k, a in enumerate(cargo_args):
        if a in ("-p", "--package") and k + 1 < len(cargo_args):
            selected.append(cargo_args[k + 1])
    packages = [p for p in meta["packages"] if p["name"] in selected]
    if len(packages) != len(set(selected)):
        say("openshell-runner: selected package missing from cargo metadata")
        return 2

    build = subprocess.Popen(
        ["cargo", "build", "--tests", "--keep-going", "--locked", "--offline",
         "--message-format=json-render-diagnostics"] + cargo_args,
        stdout=subprocess.PIPE,
        text=True,
    )
    built = {}
    for raw in build.stdout:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("reason") == "compiler-artifact" and msg.get("profile", {}).get("test") and msg.get("executable"):
            t = msg["target"]
            built[(msg["package_id"], t["name"], target_group(t["kind"]))] = msg["executable"]
    build_rc = build.wait()

    plan = []
    for pkg in sorted(packages, key=lambda p: p["name"]):
        targets = [t for t in pkg["targets"] if t.get("test", True) and target_group(t["kind"]) in ("lib", "bin", "test")]
        order = {"lib": 0, "bin": 1, "test": 2}
        targets.sort(key=lambda t: (order[target_group(t["kind"])], t["name"]))
        for t in targets:
            plan.append((pkg, t, built.get((pkg["id"], t["name"], target_group(t["kind"])))))

    failed_targets = [(p, t) for p, t, exe in plan if exe is None]
    if opts.check:
        for p, t in failed_targets:
            say(f"openshell-runner: test target {p['name']}::{t['name']} did not build")
        return 0 if build_rc == 0 and not failed_targets else 1

    rc = 0 if build_rc == 0 else 101
    applied = test_patch_applied(repo) if failed_targets else False
    touched = {}
    if applied:
        with open(TEST_PATCH, encoding="utf-8", errors="replace") as fh:
            touched = {os.path.realpath(os.path.join(repo, k)): v for k, v in changed_lines(fh.read()).items()}
    keep = libtest_filter(opts.libtest)

    for pkg, t, exe in plan:
        pkg_dir = os.path.dirname(pkg["manifest_path"])
        rel_src = os.path.relpath(t["src_path"], pkg_dir)
        group = target_group(t["kind"])
        label = f"unittests {rel_src}" if group in ("lib", "bin") else rel_src
        crate = t["name"].replace("-", "_")
        if exe is not None:
            say(f"     Running {label} ({os.path.relpath(exe, repo)})")
            env = dict(os.environ)
            version = pkg["version"]
            vparts = (version.split("-")[0].split(".") + ["0", "0", "0"])[:3]
            env.update(
                CARGO=env.get("CARGO", "cargo"),
                CARGO_MANIFEST_DIR=pkg_dir,
                CARGO_MANIFEST_PATH=pkg["manifest_path"],
                CARGO_PKG_NAME=pkg["name"],
                CARGO_PKG_VERSION=version,
                CARGO_PKG_VERSION_MAJOR=vparts[0],
                CARGO_PKG_VERSION_MINOR=vparts[1],
                CARGO_PKG_VERSION_PATCH=vparts[2],
                CARGO_CRATE_NAME=crate,
            )
            libpath = [os.path.join(target_dir, "debug", "deps"), os.path.join(target_dir, "debug")]
            if env.get("LD_LIBRARY_PATH"):
                libpath.append(env["LD_LIBRARY_PATH"])
            env["LD_LIBRARY_PATH"] = ":".join(libpath)
            r = subprocess.run([exe] + opts.libtest, cwd=pkg_dir, env=env)
            if r.returncode != 0:
                rc = 101
            continue

        say(f"     Running {label} (target/debug/deps/{crate})")
        say(f"openshell-runner: test target {pkg['name']}::{t['name']} did not compile")
        rc = 101
        if not applied:
            continue
        names = set()
        for path, modpath, rf in module_tree(t["src_path"]):
            lines = touched.get(path)
            if not lines:
                continue
            for mods, fn, first, last, ignored in rf.tests():
                if not any(first <= ln <= last for ln in lines):
                    continue
                name = "::".join(modpath + mods + [fn])
                if keep(name, ignored):
                    names.add(name)
        if names:
            say("openshell-runner: reporting the tests test.patch adds or modifies in it as FAILED")
        for name in sorted(names):
            say(f"test {name} ... FAILED")

    if opts.doc:
        r = subprocess.run(
            ["cargo", "test", "--doc", "--no-fail-fast", "--locked", "--offline"] + cargo_args
            + (["--"] + opts.libtest if opts.libtest else [])
        )
        if r.returncode != 0:
            rc = 101
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''
_SERVER = "openshell-server"


def _patch_paths(patch: str) -> list[tuple[str, str]]:
    return re.findall(r"^diff --git a/(\S+) b/(\S+)", patch, flags=re.M)


def _new_crates(patch: str) -> set[str]:
    """Crates whose Cargo.toml the patch creates (absent at base.sha)."""
    new = set()
    for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
        head = re.match(r"diff --git a/crates/([^/\s]+)/Cargo\.toml ", block)
        if head and re.search(r"^new file mode", block, re.M):
            new.add(head.group(1))
    return new


def _crates(pr: PullRequest) -> list[str]:
    """Workspace crates (crates/<name>, package name == dir name) touched by
    test.patch or fix.patch. The e2e/ and tests/ manifests need a live
    gateway/cluster and are out of scope."""
    crates: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for a, b in _patch_paths(patch):
            for path in (a, b):
                found = re.match(r"crates/([^/]+)/", path)
                if found:
                    crates.add(found.group(1))
    crates -= _new_crates(pr.fix_patch) | _new_crates(pr.test_patch)
    if not crates:
        crates.add(_SERVER)
    return sorted(crates)


def _cargo_args(pr: PullRequest) -> str:
    crates = _crates(pr)
    args = " ".join(f"-p {shlex.quote(c)}" for c in crates)
    if _SERVER in crates:
        # The repo's test:rust task runs openshell-server with test-support.
        args += f" --features {_SERVER}/test-support"
    return args


def _suites(pr: PullRequest) -> list[tuple[str, str]]:
    # Each suite goes through /home/openshell_tests.py (see _RUNNER): it builds
    # every test target with `cargo build --tests --keep-going` and runs each
    # built test binary as `cargo test` would, so one test target that does
    # not compile (test.patch using API that only fix.patch adds) does not
    # stop the others.
    args = shlex.quote(_cargo_args(pr))
    suites = [
        ("unit", f"python3 /home/openshell_tests.py --doc --cargo-args {args}"),
    ]
    if _SERVER in _crates(pr):
        # tasks/scripts/run-postgres-tests.sh: the #[ignore]d postgres_* tests,
        # one at a time (advisory locks are database-wide); bench_postgres_*
        # capacity checks are excluded as in CI. Same package/feature
        # selection as the unit suite, so the prepare.sh pre-build covers it.
        suites.append(
            (
                "postgres",
                f"python3 /home/openshell_tests.py --cargo-args {args} -- "
                f"--ignored --test-threads 1 postgres_ --skip bench_postgres_",
            )
        )
    return suites


def _test_commands(pr: PullRequest) -> str:
    lines = ["rc=0"]
    for name, cmd in _suites(pr):
        lines.append(f'echo "{_SUITE_MARK} {name}"')
        if name == "postgres":
            lines.append(_PG_START)
            lines.append(f"export OPENSHELL_TEST_POSTGRES_URL={_PG_URL}")
            lines.append(f"export OPENSHELL_REPLAY_TEST_DATABASE_URL={_PG_URL}")
        lines.append(f"{cmd} || rc=$?")
    lines.append('exit "$rc"')
    return "\n".join(lines)


def _apply_flags(pr: PullRequest) -> str:
    skipped: set[str] = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for block in re.split(r"^(?=diff --git )", patch, flags=re.M):
            head = re.match(r"diff --git a/(\S+) b/(\S+)", block)
            if (
                head
                and re.search(r"^Binary files .* differ$", block, re.M)
                and "GIT binary patch" not in block
            ):
                skipped.update(head.groups())
    flags = ["--whitespace=nowarn"] + [
        f"--exclude={shlex.quote(path)}" for path in sorted(skipped)
    ]
    return " ".join(flags)


class OpenShellImageBase(Image):
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
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # From deploy/docker/Dockerfile.ci: clang/libclang-dev + libz3-dev for
        # the z3 crate (openshell-prover), libssl-dev/pkg-config, cmake for
        # aws-lc-sys, openssh-client and socat used by tests. postgresql stands
        # in for the disposable PostgreSQL container of test:rust:postgres.
        return [
            "pkg-config",
            "libssl-dev",
            "clang",
            "libclang-dev",
            "libz3-dev",
            "cmake",
            "openssh-client",
            "socat",
            "unzip",
            "xz-utils",
            "jq",
            "postgresql",
            "postgresql-client",
        ]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    RUST_BACKTRACE=1 \\\n"
            "    CI=true\n"
            "RUN git config --global --add safe.directory '*' && rustc --version && cargo --version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. Per-PR base, so the pin and
    # the history scrub belong here.


class OpenShellImageDefault(Image):
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
        return OpenShellImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        tests = _test_commands(self.pr)
        apply = _apply_flags(self.pr)
        cargo_args = _cargo_args(self.pr)
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
            File(".", "openshell_tests.py", _RUNNER),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -euo pipefail

# Section 1: pin
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2: provision
{_ENV}

fetched=0
for attempt in 1 2 3 4 5; do
  if cargo fetch --locked; then
    fetched=1
    break
  fi
  sleep 20
done
test "$fetched" = 1

# Pre-build every test binary the run scripts use, at this commit.
cargo test --locked --offline --no-run {cargo_args}

# Disposable PostgreSQL role/database (superuser, as POSTGRES_USER is in the
# official image the repo's runner uses).
{_PG_START}
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -c "CREATE ROLE openshell LOGIN SUPERUSER PASSWORD 'openshell'"
runuser -u postgres -- createdb -O openshell openshell
psql "{_PG_URL}" -v ON_ERROR_STOP=1 -tAc "SELECT 1" | grep -qx 1
pg_ctlcluster "$pg_ver" main stop

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
cargo test --locked --offline {cargo_args} -- --list > /tmp/openshell-test-list.txt
grep -qE ': test$' /tmp/openshell-test-list.txt
rm -f /tmp/openshell-test-list.txt
# Every test target the runner will execute builds at this commit.
python3 /home/openshell_tests.py --check --cargo-args {shlex.quote(cargo_args)}
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{tests}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch
{tests}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply {apply} /home/test.patch /home/fix.patch
{tests}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("NVIDIA", "OpenShell")
class OPENSHELL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenShellImageDefault(self.pr, self._config)

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

        suite_re = re.compile(rf"^{re.escape(_SUITE_MARK)} (\S+)\s*$")
        # "Running unittests src/lib.rs (target/debug/deps/openshell_server-0123abcd)"
        # "Running tests/foo.rs (target/debug/deps/foo-0123abcd)"
        running_re = re.compile(
            r"^Running (?:unittests )?(\S+) \((?:.*/)?([A-Za-z0-9_]+?)(?:-[0-9a-f]{8,})?(?:\.exe)?\)\s*$"
        )
        doctest_re = re.compile(r"^Doc-tests (\S+)\s*$")
        result_re = re.compile(r"^test (\S.*?) \.\.\. (ok|FAILED|ignored)\b")

        suite = ""
        target = ""
        for raw in log.splitlines():
            line = raw.rstrip("\r")
            stripped = line.strip()
            found = suite_re.match(stripped)
            if found:
                suite = found.group(1)
                target = ""
                continue
            found = running_re.match(stripped)
            if found:
                target = f"{found.group(2)}[{found.group(1)}]"
                continue
            found = doctest_re.match(stripped)
            if found:
                target = f"{found.group(1)}[doc]"
                continue
            found = result_re.match(stripped)
            if not found or not target:
                continue
            name = re.sub(r" \(line \d+\)", "", found.group(1)).strip()
            test = f"{suite}/{target}::{name}" if suite else f"{target}::{name}"
            status = found.group(2)
            if status == "ok":
                passed_tests.add(test)
            elif status == "FAILED":
                failed_tests.add(test)
            else:
                skipped_tests.add(test)

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
