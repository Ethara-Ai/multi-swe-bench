"""Utility to strip binary file diffs from patches.

Graphite patches were generated without --full-index, so binary file diffs
(renames, deletions, copies of .png/.jpg/.ico etc.) have abbreviated index
hashes that git-apply cannot resolve.  Since tests never depend on binary
assets, we simply remove those diff sections before writing the patch files
into the Docker image.
"""

import re


def filter_binary_diffs(patch_text: str) -> str:
    """Remove diff sections that touch binary files or are pure renames
    without text hunks.

    Keeps every section that contains at least one ``@@`` text hunk.
    Drops sections that are:
      - ``Binary files … differ`` (binary content change)
      - Pure rename / copy of a binary file (no text hunk)
    """
    if not patch_text:
        return patch_text

    parts = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    filtered: list[str] = []
    for part in parts:
        if not part.startswith("diff --git"):
            filtered.append(part)
            continue

        has_hunk = bool(re.search(r"^@@", part, re.MULTILINE))
        has_binary_marker = bool(
            re.search(r"^Binary files ", part, re.MULTILINE)
        )

        if has_hunk:
            # Text changes present — keep the section but strip any stray
            # ``Binary files …`` lines that git-apply would choke on.
            lines = part.split("\n")
            cleaned = [l for l in lines if not l.startswith("Binary files ")]
            filtered.append("\n".join(cleaned))
        elif has_binary_marker:
            # Pure binary diff — skip entirely.
            continue
        elif "rename from" in part or "copy from" in part:
            # Pure rename/copy without any text hunk — skip (cannot apply
            # without full index on binary files).
            continue
        else:
            # Other (e.g. mode change only) — keep.
            filtered.append(part)

    return "".join(filtered)


def get_binary_new_files(patch_text: str) -> list[str]:
    """Extract paths of binary files created from /dev/null.

    These files are stripped by ``filter_binary_diffs`` but some are required
    for compilation (e.g. ``null.png`` used by ``include_bytes!``).  Returns
    paths so callers can create empty placeholders in the Docker image.
    """
    if not patch_text:
        return []

    parts = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    new_files: list[str] = []
    for part in parts:
        if not part.startswith("diff --git"):
            continue
        is_new = "new file" in part or "Binary files /dev/null" in part
        is_binary = bool(re.search(r"^Binary files ", part, re.MULTILINE)) or \
                    "GIT binary patch" in part
        if is_new and is_binary:
            m = re.match(r"diff --git a/\S+ b/(\S+)", part)
            if m:
                new_files.append(m.group(1))
    return new_files
