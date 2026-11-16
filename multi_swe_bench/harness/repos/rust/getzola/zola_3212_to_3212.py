"""getzola/zola -- Cargo workspace, graded per touched component (PRs 3212, 3237).

Snapshot (insta) tests run with CI=true, so a snapshot mismatch fails instead of being rewritten. test_site/, docs/ and .github/ files in a test patch are fixtures/CI files of the root package and do not select it for grading.

Architecture: PER-PR BASE. ImageBase.image_tag() is f"base-pr-{N}" and the base
inherits the harness default Image.dockerfile(), which clones "${REPO_URL}",
checks out ${BASE_COMMIT} and runs Image._HARDENING_BLOCK (pin + history scrub)
in the base layer. DockerfileEnhancer injects the build args / proxy / CA trust
into that base. The PR image (never enhanced) only COPYs the scripts and RUNs
prepare.sh once.

Toolchain: rust:1.97.1-bookworm (official multi-arch amd64 + arm64 image) and
RUSTUP_TOOLCHAIN=1.97.1, so a `channel = "stable"` rust-toolchain file
can never pull a newer compiler at test time. CI uses dtolnay/rust-toolchain@stable and the repo pins no toolchain; 1.97.1 was the stable release at base commits 6a92a76edb / 7fe17abff2 (2026-08-06 / 2026-08-11, PRs 3212, 3237).

Grading scope: every stage runs ONE shared command (/home/run_tests.sh ->
/home/msb_cargo.py run). It tests only the Cargo packages that own a file the
test patch touches (read from /home/test.patch's file headers; nothing is
applied outside test-run.sh / fix-run.sh): `cargo build --tests --keep-going`,
then `cargo test --no-fail-fast` on every test target that compiled. Test ids
are "<repo-relative source file>::<libtest path>", the file resolved from the
test binary's crate root and the test's module path. A test target that does
not compile at a stage is skipped there, and the test functions the test patch
adds or edits inside it are reported as FAIL.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "getzola"
_REPO = "zola"
_REGISTER_KEY = "zola_3212_to_3212"

# Single-PR routing (DS3 multi-arch set). Dataset rows carry no number_interval, so without
# this the row would resolve to the "{org}/{repo}" catch-all config. Only this exact PR
# number of this repo is tagged with this file's interval; every other PR is unaffected.
from multi_swe_bench.harness import pull_request as _pull_request

_INTERVAL = _REGISTER_KEY
_BOUNDS = re.search(r"_(\d+)_to_(\d+)$", _INTERVAL)
_PR_MIN = int(_BOUNDS.group(1))
_PR_MAX = int(_BOUNDS.group(2))
_ROUTING_FLAG = "_" + _INTERVAL + "_routed"

if not getattr(_pull_request.PullRequest, _ROUTING_FLAG, False):
    _orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _routed_from_json(cls, json_str):
        pr = _orig_from_json(cls, json_str)
        if (
            pr.org == _ORG
            and pr.repo == _REPO
            and not pr.number_interval
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return pr

    _pull_request.PullRequest.from_json = classmethod(_routed_from_json)
    setattr(_pull_request.PullRequest, _ROUTING_FLAG, True)
_RUST_IMAGE = "rust:1.97.1-bookworm"


_CHECK_GIT_CHANGES = r"""#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_MSB_ENV = r"""# Sourced by prepare.sh and run_tests.sh -- one definition of the toolchain.
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export RUST_BACKTRACE=1
# 8 MiB per test thread instead of libtest's 2 MiB default: deep recursion in an
# unoptimised (debug) test build otherwise aborts the WHOLE test binary with a
# stack overflow, losing every result after it (utoipa's justfile sets the same).
export RUST_MIN_STACK=8388608
# Line tables only (backtraces keep file:line): full debuginfo makes the debug
# target/ of a large workspace tens of GB per image.
export CARGO_PROFILE_DEV_DEBUG=line-tables-only
export CARGO_PROFILE_TEST_DEBUG=line-tables-only
export RUSTUP_TOOLCHAIN="1.97.1"
export MSB_TIMEOUT_SECS=1550
"""

_MSB_CARGO = r"""#!/usr/bin/env python3
# Cargo driver shared by prepare.sh (provision / gate) and run_tests.sh (run).
#
# Scope: the Cargo packages that own a file the test patch touches (read from the
# file headers of /home/test.patch only -- nothing is applied here), plus the
# config's extra packages, minus its excluded ones. Every stage runs the same
# command over the same packages.
#
#   provision  cargo fetch + build every test target of the graded packages once
#              (fills ~/.cargo and target/ so the stages do not recompile the world)
#   gate       the same build, --offline; must succeed (hard gate)
#   run        build the test targets with --keep-going, run `cargo test` on every
#              target that compiled, and print one line per test:
#                  MSB_TESTCASE <tab> <repo-relative source file> <tab> <libtest path> <tab> PASS|FAIL|SKIP
#              The source file is resolved from the test binary's crate root and
#              the test's module path, so a unit test in src/a/tests.rs reports
#              src/a/tests.rs, an integration test reports tests/<file>.rs.
#              A target that does not compile at this stage (a gold test using an
#              API the fix adds) is skipped, and the test functions the test patch
#              adds or edits in it are reported as FAIL -- a gold test that cannot
#              compile is a failing test, not an absent one.
#
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

REPO = "/home/@@REPO@@"
CFG = json.loads(r'''{"ignore_files": [".github/*", "test_site/*", "docs/*"]}''')
TIMEOUT = int(os.environ.get("MSB_TIMEOUT_SECS", "1500"))
DEADLINE = time.time() + TIMEOUT
FLAGS = CFG.get("cargo_flags", [])
TEST_ARGS = CFG.get("test_args", [])


def log(*a):
    print(*a, flush=True)


def remaining():
    return max(60, int(DEADLINE - time.time()))


def patch_files(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def is_package_manifest(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return re.search(r"^\s*\[package\]\s*$", f.read(), re.M) is not None
    except OSError:
        return False


def owning_package_dir(rel):
    d = os.path.dirname(rel)
    while True:
        cand = os.path.join(REPO, d, "Cargo.toml") if d else os.path.join(REPO, "Cargo.toml")
        if is_package_manifest(cand):
            return d or "."
        if not d:
            return None
        d = os.path.dirname(d)


def excluded(d):
    return any(fnmatch.fnmatch(d, g) for g in CFG.get("exclude", []))


def select_package_dirs():
    dirs = []
    if CFG.get("packages"):
        dirs = list(CFG["packages"])
    else:
        for rel in patch_files("/home/test.patch"):
            if any(fnmatch.fnmatch(rel, g) for g in CFG.get("ignore_files", [])):
                continue
            d = owning_package_dir(rel)
            if d and d not in dirs:
                dirs.append(d)
    for d in CFG.get("extra_packages", []):
        if d not in dirs:
            dirs.append(d)
    return [d for d in dirs if not excluded(d) and is_package_manifest(os.path.join(REPO, d, "Cargo.toml"))]


def sh(cmd, cwd, timeout=None, capture=False):
    log("+ (cd %s && %s)" % (os.path.relpath(cwd, REPO) if cwd != REPO else ".", " ".join(cmd)))
    if capture:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, stdout=subprocess.PIPE, text=True)
    return subprocess.run(cmd, cwd=cwd, timeout=timeout)


def package_info(dirs):
    # Group the selected package dirs by workspace root -> [(dir, name)].
    groups = {}
    for d in dirs:
        man = os.path.join(REPO, d, "Cargo.toml")
        ws = subprocess.run(["cargo", "locate-project", "--workspace", "--message-format", "plain",
                             "--manifest-path", man], stdout=subprocess.PIPE, text=True, cwd=REPO)
        root = os.path.dirname(ws.stdout.strip()) if ws.returncode == 0 and ws.stdout.strip() else os.path.dirname(man)
        name = None
        with open(man, encoding="utf-8", errors="replace") as f:
            txt = f.read()
        m = re.search(r"^\s*\[package\]\s*$(.*?)(?=^\s*\[|\Z)", txt, re.M | re.S)
        if m:
            n = re.search(r'^\s*name\s*=\s*"([^"]+)"', m.group(1), re.M)
            name = n.group(1) if n else None
        if not name:
            log("MSB_PACKAGE_WITHOUT_NAME", d)
            continue
        groups.setdefault(root, []).append((d, name))
    return groups


def build(root, pkgs, flags, targets=None, offline=False):
    # cargo build --tests --keep-going; returns (rc, artifacts, failed_targets).
    cmd = ["cargo", "build", "--tests", "--keep-going", "--message-format=json-render-diagnostics"]
    if offline:
        cmd.append("--offline")
    cmd += flags
    for _, n in pkgs:
        cmd += ["-p", n]
    if targets:
        cmd += targets
    log("+ (cd %s && %s)" % (os.path.relpath(root, REPO), " ".join(cmd)))
    p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, text=True, errors="replace")
    artifacts, failed = {}, {}
    try:
        for line in p.stdout:
            if not line.startswith("{"):
                sys.stdout.write(line)
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            tgt = msg.get("target") or {}
            if msg.get("reason") == "compiler-artifact" and (msg.get("profile") or {}).get("test") and msg.get("executable"):
                artifacts[os.path.basename(msg["executable"])] = {
                    "pkg": os.path.normpath(os.path.dirname(msg.get("manifest_path") or "")),
                    "kind": tgt.get("kind", ["?"])[0], "name": tgt.get("name"), "src": tgt.get("src_path"),
                }
        p.wait(timeout=remaining())
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        log("MSB_BUILD_TIMEOUT")
        return 124, artifacts, failed
    if p.returncode != 0:
        # json-render-diagnostics keeps rustc errors out of the JSON stream, so a
        # target that failed to compile is one the manifest declares as testable
        # (and this build asked for) but that produced no test executable.
        built = {(a["pkg"], a["name"], a["kind"]) for a in artifacts.values()}
        for pdir, tname, kind, src in testable_targets(root, pkgs, flags):
            if targets and tuple(kind_flag({"kind": kind, "name": tname})) not in flag_tuples(targets):
                continue
            if (pdir, tname, kind) not in built:
                failed[(pdir, tname, kind)] = src
    return p.returncode, artifacts, failed


def flag_tuples(targets):
    out, i = set(), 0
    while i < len(targets):
        if targets[i] in ("--lib", "--bins", "--tests"):
            out.add((targets[i],))
            i += 1
        else:
            out.add((targets[i], targets[i + 1]))
            i += 2
    return out


def testable_targets(root, pkgs, flags):
    r = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1", "--offline"],
                       cwd=root, stdout=subprocess.PIPE, text=True)
    if r.returncode != 0:
        return []
    wanted = {os.path.normpath(os.path.join(REPO, d)) for d, _ in pkgs}
    out = []
    for pk in json.loads(r.stdout).get("packages", []):
        pdir = os.path.normpath(os.path.dirname(pk["manifest_path"]))
        if pdir not in wanted:
            continue
        for t in pk.get("targets", []):
            kind = t.get("kind", ["?"])[0]
            if not t.get("test", True) or kind not in ("lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro", "bin", "test"):
                continue
            feats = t.get("required-features") or []
            if feats and "--all-features" not in flags and not all(
                    any(f in fl for fl in flags if fl.startswith("--features") or not fl.startswith("-")) for f in feats):
                continue
            out.append((pdir, t["name"], kind, t["src_path"]))
    return out


# ---------------------------------------------------------------- run helpers
RUNNING_RE = re.compile(r"^\s*Running (?:unittests )?\S.*? \((.+?)\)\s*$")
TEST_RE = re.compile(r"(?:^|\s)test (\S+?)(?: - should panic)? \.\.\. (ok|FAILED|ignored)\b")


def resolve_file(src_root, test_path):
    # Map a libtest path (mod::sub::test_fn) to the source file defining it.
    segs = test_path.split("::")[:-1]
    cur = src_root
    d = os.path.dirname(src_root)
    for seg in segs:
        hit = None
        for cand in (os.path.join(d, seg + ".rs"), os.path.join(d, seg, "mod.rs")):
            if os.path.isfile(cand):
                hit = cand
                break
        if not hit:
            break  # inline module: everything below lives in `cur`
        cur = hit
        d = os.path.dirname(hit) if hit.endswith("mod.rs") else os.path.join(d, seg)
    return os.path.relpath(cur, REPO)


def module_path_of(src_root, file_abs):
    # Inverse of resolve_file for a file under the crate root's directory.
    base = os.path.dirname(src_root)
    if os.path.abspath(file_abs) == os.path.abspath(src_root):
        return []
    rel = os.path.relpath(file_abs, base)
    if rel.startswith(".."):
        return None
    parts = rel[:-3].split(os.sep) if rel.endswith(".rs") else rel.split(os.sep)
    if parts and parts[-1] == "mod":
        parts = parts[:-1]
    return parts


# ---------------------------------------------------- gold tests of a broken target
TEST_ATTR_RE = re.compile(r"^\s*#\[\s*(?:[A-Za-z_][\w]*::)*test\s*(?:\]|\()")
FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)")
MOD_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*\{")


def added_lines_by_file():
    out, cur, ln = {}, None, 0
    if not os.path.exists("/home/test.patch"):
        return out
    with open("/home/test.patch", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("diff --git "):
                m = re.match(r"^diff --git a/.* b/(.*?)\s*$", line)
                cur = m.group(1) if m else None
                continue
            if line.startswith("@@"):
                m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
                ln = int(m.group(1)) if m else 0
                continue
            if cur is None or line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                out.setdefault(cur, set()).add(ln)
                ln += 1
            elif line.startswith(" "):
                ln += 1
    return out


def strip_code(line):
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    line = re.sub(r"'(?:\\.|[^'\\])'", "''", line)
    return line.split("//", 1)[0]


def gold_tests_in_file(path_abs, touched_lines):
    # [(inline-module path, fn name)] of #[test]-style fns overlapping touched lines.
    try:
        lines = open(path_abs, encoding="utf-8", errors="replace").read().split("\n")
    except OSError:
        return []
    res, stack, depth, pending_attr, current = [], [], 0, False, None
    for i, raw in enumerate(lines, start=1):
        code = strip_code(raw)
        if TEST_ATTR_RE.match(code):
            pending_attr = True
        m_mod = MOD_RE.match(code)
        m_fn = FN_RE.match(code)
        if m_fn and pending_attr and current is None:
            current = {"name": m_fn.group(1), "mods": [s[0] for s in stack], "start": i, "depth": depth}
            pending_attr = False
        elif m_fn:
            pending_attr = False
        opens, closes = code.count("{"), code.count("}")
        if m_mod:
            stack.append((m_mod.group(1), depth))
        depth += opens - closes
        while stack and depth <= stack[-1][1]:
            stack.pop()
        if current is not None and depth <= current["depth"] and i > current["start"] - 1 and ("}" in code or ";" in code):
            if any(current["start"] - 3 <= l <= i for l in touched_lines):
                res.append((current["mods"], current["name"]))
            current = None
    return res


def declares_chain(src_root, mods):
    # True when every step of crate-root -> mods is a `mod x;` declaration (or the file is the root).
    cur, d = src_root, os.path.dirname(src_root)
    for seg in mods:
        try:
            txt = strip_all(open(cur, encoding="utf-8", errors="replace").read())
        except OSError:
            return False
        if not re.search(r"\bmod\s+%s\s*;" % re.escape(seg), txt):
            return False
        nxt = None
        for cand in (os.path.join(d, seg + ".rs"), os.path.join(d, seg, "mod.rs")):
            if os.path.isfile(cand):
                nxt = cand
                break
        if nxt is None:
            return False
        cur = nxt
        d = os.path.dirname(nxt) if nxt.endswith("mod.rs") else os.path.join(d, seg)
    return True


def strip_all(text):
    return "\n".join(strip_code(l) for l in text.split("\n"))


def synthesize_failures(failed, results):
    touched = added_lines_by_file()
    printed = set()
    for _key, src in failed.items():
        if not src:
            continue
        for rel, lines in touched.items():
            if not rel.endswith(".rs"):
                continue
            ab = os.path.join(REPO, rel)
            if not os.path.isfile(ab):
                continue
            mods = module_path_of(src, ab)
            if mods is None or not declares_chain(src, mods):
                continue
            for inline, fn in gold_tests_in_file(ab, lines):
                path = "::".join(mods + inline + [fn])
                key = (rel, path)
                if key in printed or key in results:
                    continue
                printed.add(key)
                log("MSB_TESTCASE\t%s\t%s\tFAIL" % (rel, path))
    if printed:
        log("MSB_SYNTHESIZED_COMPILE_FAILURES %d" % len(printed))


def kind_flag(a):
    k = a["kind"]
    if k in ("lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"):
        return ["--lib"]
    if k == "bin":
        return ["--bin", a["name"]]
    if k == "test":
        return ["--test", a["name"]]
    return []


def touched_rs_files():
    out = []
    for rel in patch_files("/home/test.patch"):
        ab = os.path.join(REPO, rel)
        if rel.endswith(".rs") and os.path.isfile(ab):
            out.append(ab)
    return out


def plans():
    # [(workspace root, [(dir, name)], cargo flags, explicit target flags or None)]
    out = []
    if CFG.get("runs"):
        for r in CFG["runs"]:
            dirs = [d for d in r["packages"] if is_package_manifest(os.path.join(REPO, d, "Cargo.toml"))]
            for root, pkgs in package_info(dirs).items():
                out.append((root, pkgs, r.get("cargo_flags", []), r.get("targets")))
        return out
    dirs = select_package_dirs()
    for root, pkgs in package_info(dirs).items():
        if not CFG.get("touched_targets_only"):
            out.append((root, pkgs, FLAGS, None))
            continue
        # Only the test targets whose module tree contains a file the test patch
        # touches (a crate too large to test whole inside the stage timeout).
        files = touched_rs_files()
        for d, n in pkgs:
            sel = []
            for pdir, tname, kind, src in testable_targets(root, [(d, n)], FLAGS):
                for ab in files:
                    mods = module_path_of(src, ab)
                    if mods is not None and declares_chain(src, mods):
                        f = kind_flag({"kind": kind, "name": tname})
                        if f and tuple(f) not in flag_tuples(sel):
                            sel += f
                        break
            log("MSB_TOUCHED_TARGETS %s: %s" % (d, " ".join(sel) or "(none at this stage)"))
            if sel:
                out.append((root, [(d, n)], FLAGS, sel))
    return out


def run_tests(root, pname, flags, targets, artifacts, results):
    cmd = ["cargo", "test", "-p", pname, "--no-fail-fast"] + flags + targets + ["--"] + TEST_ARGS
    log("+ (cd %s && %s)" % (os.path.relpath(root, REPO), " ".join(cmd)))
    p = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    cur = None
    try:
        for line in p.stdout:
            sys.stdout.write(line)
            m = RUNNING_RE.match(line)
            if m:
                cur = artifacts.get(os.path.basename(m.group(1)))
                continue
            if cur is None:
                continue
            for t in TEST_RE.finditer(line):
                name, st = t.group(1), t.group(2)
                key = (resolve_file(cur["src"], name), name)
                status = {"ok": "PASS", "FAILED": "FAIL", "ignored": "SKIP"}[st]
                prev = results.get(key)
                if prev == "FAIL" or (prev == "PASS" and status == "SKIP"):
                    continue
                results[key] = status
        p.wait(timeout=remaining())
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        log("MSB_TEST_TIMEOUT package=%s" % pname)
        return 124
    return 0 if p.returncode in (0, 101) else p.returncode


def run():
    todo = plans()
    log("MSB_PLANS=" + "; ".join("%s:%s %s %s" % (os.path.relpath(r, REPO), ",".join(d for d, _ in pk), " ".join(fl), " ".join(t or []))
                                 for r, pk, fl, t in todo))
    if not todo:
        log("MSB_NO_TEST_TARGETS_AT_THIS_STAGE")
        return 0
    worst, results, all_failed = 0, {}, {}
    for root, pkgs, flags, targets in todo:
        rc, artifacts, failed = build(root, pkgs, flags, targets)
        if rc != 0:
            log("MSB_BUILD_INCOMPLETE rc=%d failed_targets=%s" % (rc, sorted("%s(%s)" % (k[1], k[2]) for k in failed)))
        if rc == 124:
            worst = 124
            break
        all_failed.update(failed)
        name_of = {os.path.normpath(os.path.join(REPO, d)): n for d, n in pkgs}
        by_pkg = {}
        for a in artifacts.values():
            if a["pkg"] in name_of:
                by_pkg.setdefault(a["pkg"], []).append(a)
        for pkg_dir, arts in sorted(by_pkg.items()):
            tflags, seen = [], set()
            for a in sorted(arts, key=lambda x: (x["kind"], x["name"])):
                t = tuple(kind_flag(a))
                if t and t not in seen and (not targets or t in flag_tuples(targets)):
                    seen.add(t)
                    tflags += list(t)
            if not tflags:
                continue
            rc2 = run_tests(root, name_of[pkg_dir], flags, tflags, artifacts, results)
            if rc2 and not worst:
                worst = rc2
            if rc2 == 124:
                break
    for (rel, name), st in sorted(results.items()):
        log("MSB_TESTCASE\t%s\t%s\t%s" % (rel, name, st))
    if all_failed:
        synthesize_failures(all_failed, results)
    if worst:
        log("MSB_TEST_RUNNER_DID_NOT_COMPLETE exit=%d (results above may be partial)" % worst)
    return worst


def provision(offline=False):
    todo = plans()
    log("MSB_PLANS=" + "; ".join("%s:%s %s %s" % (os.path.relpath(r, REPO), ",".join(d for d, _ in pk), " ".join(fl), " ".join(t or []))
                                 for r, pk, fl, t in todo))
    if not todo:
        log("MSB_NO_GRADED_PACKAGE_AT_BASE: the test patch touches no file of a Cargo package")
        return 1
    fetched = set()
    for root, pkgs, flags, targets in todo:
        if not offline and root not in fetched:
            fetched.add(root)
            r = sh(["cargo", "fetch"], root, timeout=3600)
            if r.returncode != 0:
                return r.returncode
        rc, artifacts, failed = build(root, pkgs, flags, targets, offline=offline)
        if rc != 0:
            log("MSB_BASE_BUILD_FAILED rc=%d failed_targets=%s" % (rc, sorted(k[1] for k in failed)))
            return rc
        log("MSB_TEST_TARGETS_BUILT=%d (%s)" % (len(artifacts), os.path.relpath(root, REPO)))
    return 0


if __name__ == "__main__":
    os.chdir(REPO)
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "run":
        sys.exit(run())
    if mode == "provision":
        sys.exit(provision())
    if mode == "gate":
        sys.exit(provision(offline=True))
    sys.exit("unknown mode " + mode)
"""

_PREPARE = r"""#!/bin/bash
set -e

# ---- Section 1: PIN ---------------------------------------------------------
cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach @@SHA@@
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION (the tree may get dirty from here on) -------------
source /home/msb_env.sh

rustc --version
# cargo fetch + one build of every test target of the graded packages.
python3 /home/msb_cargo.py provision

# ---- Section 3: GATE (hard, never tolerated) --------------------------------
python3 /home/msb_cargo.py gate
echo "DEPS_OK"
"""

_RUN_TESTS = r"""#!/bin/bash
# The single test command shared verbatim by run.sh, test-run.sh and fix-run.sh.
# Test failures are data (reported per test); a runner that does not complete
# exits non-zero.
set -o pipefail
cd /home/@@REPO@@
source /home/msb_env.sh

python3 /home/msb_cargo.py run
"""

_APPLY_FN = r"""# Apply one patch file. Each file gets its own context ladder: full context
# first; only if the base commit moved under the patch's context (the PR
# branch was cut from an older parent than base.sha) retry with 2, then 1,
# lines. git apply is atomic per file, so a retry never meets half-applied
# hunks of the same file; if no attempt applies, the stage fails loudly.
# Entries of the form "Binary files A and B differ" (the patch was exported
# without --binary) carry no bytes at all and can never apply; only those
# paths are excluded, every other hunk is applied as-is.
_msb_apply() {
    local _ex=() _f
    while IFS= read -r _f; do
        [ -n "$_f" ] && _ex+=("--exclude=$_f")
    done < <(sed -nE 's#^Binary files (.*) and (.*) differ$#\1\n\2#p' "$1" \
                 | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | sort -u)
    git apply --whitespace=nowarn "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C2 "${_ex[@]}" "$1" \
        || git apply --whitespace=nowarn -C1 "${_ex[@]}" "$1"
}
"""


def _apply(patches: str) -> str:
    return "\n".join(f"_msb_apply {p}" for p in patches.split())


def _stage(apply_line: str) -> str:
    return (
        "#!/bin/bash\n"
        "set -eo pipefail\n"
        "export CI=true\n"
        "\n"
        + (_APPLY_FN + "\n" if apply_line else "")
        + "cd /home/@@REPO@@\n"
        + (apply_line + "\n" if apply_line else "")
        + "bash /home/run_tests.sh\n"
    )


_RUN = _stage("")
_TEST_RUN = _stage(_apply("/home/test.patch"))
_FIX_RUN = _stage(_apply("/home/test.patch /home/fix.patch"))


def _render(body: str, pr: PullRequest) -> str:
    return body.replace("@@REPO@@", pr.repo).replace("@@SHA@@", pr.base.sha)


class ZolaImageBase(Image):
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
        # PR-scoped: the default dockerfile() pins ${BASE_COMMIT} and scrubs
        # history in this layer, so it must never be shared across PRs.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["python3"]

    def extra_setup(self) -> str:
        return ""


class ZolaImageDefault(Image):
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
        return ZolaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "msb_env.sh", _render(_MSB_ENV, pr)),
            File(".", "msb_cargo.py", _render(_MSB_CARGO, pr)),
            File(".", "prepare.sh", _render(_PREPARE, pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_TEST_RUN, pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN, pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


_CASE_RE = re.compile(r"^MSB_TESTCASE\t([^\t\n]+)\t([^\t\n]+)\t(PASS|FAIL|SKIP)[ \t\r]*$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DIFF_B_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)


@Instance.register(_ORG, _REGISTER_KEY)
class Zola(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ZolaImageDefault(self.pr, self._config)

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
        clean = _ANSI_RE.sub("", test_log)

        # Tests defined in a file the FIX patch writes (Rust keeps unit tests in
        # the source file they cover) are the fix's own tests, not the gold
        # test patch's: crediting them would grade the fix against itself, so
        # they are not reported at any stage. Gold test files are never in the
        # fix patch.
        fix_files = set(_DIFF_B_RE.findall(self.pr.fix_patch or "")) - set(
            _DIFF_B_RE.findall(self.pr.test_patch or "")
        )

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # "<repo-relative source file>::<test path>": report.py splits on the
        # first "::" to map a credited test back to the file that defines it.
        for path, name, status in _CASE_RE.findall(clean):
            if path in fix_files:
                continue
            test_id = f"{path}::{name}"
            if status == "FAIL":
                failed_tests.add(test_id)
            elif status == "SKIP":
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Failure wins over pass, pass wins over skip; the buckets never overlap.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
