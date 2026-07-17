"""Shared Bazel test-target extraction (ported from bazel.py BazelImageDefault).

Instead of running the whole `//src/test/...` tree (builds + runs thousands of
unrelated targets -> hours per run), we derive only the test targets that the
PR's test_patch actually touches. This is both far faster AND more correct for
f2p/n2p measurement. Falls back to `//src/test/...` when nothing can be derived.
"""
import re


def _find_build_dirs(*patches: str) -> set:
    dirs = set()
    for patch in patches:
        if not patch:
            continue
        for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch):
            path = m.group(2)
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename in ("BUILD", "BUILD.bazel"):
                pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                dirs.add(pkg_dir)
    return dirs


def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set) -> bool:
    if not parent_dir:
        return False
    if "/test/py/" in pkg_dir:
        return True
    if "/testdata/" in pkg_dir:
        return True
    if pkg_dir.endswith("/testdata"):
        return True
    if pkg_dir.endswith("/bin"):
        return True
    if parent_dir in build_dirs:
        return True
    return False


def extract_test_targets(test_patch: str, fix_patch: str = "") -> str:
    """Return a space-separated set of bazel test targets, or //src/test/... ."""
    all_build_dirs = _find_build_dirs(test_patch or "", fix_patch or "")

    test_files = []
    for m in re.finditer(r"diff --git a/(.+?) b/(.+)", test_patch or ""):
        path = m.group(2)
        basename = path.rsplit("/", 1)[-1] if "/" in path else path
        if "/test/" not in path and "/javatests/" not in path:
            continue
        if basename in ("BUILD", "BUILD.bazel"):
            continue
        test_files.append(path)

    if not test_files:
        return "//src/test/..."

    targets = set()
    for path in test_files:
        pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        basename = path.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename

        if pkg_dir in all_build_dirs:
            targets.add(f"//{pkg_dir}/...")
            continue

        parent = pkg_dir
        found_parent_pkg = False
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in all_build_dirs:
                targets.add(f"//{parent}:{stem}")
                found_parent_pkg = True
                break
        if found_parent_pkg:
            continue

        parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
        if _likely_subdir(pkg_dir, parent_dir, all_build_dirs):
            targets.add(f"//{parent_dir}:{stem}")
        else:
            targets.add(f"//{pkg_dir}/...")

    if not targets:
        return "//src/test/..."
    return " ".join(sorted(targets))
