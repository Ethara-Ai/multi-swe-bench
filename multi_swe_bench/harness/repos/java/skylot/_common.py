"""Helpers for skylot/jadx config variants.

The upstream lht release-line patches often reference binary files via the
short-form `Binary files a/X and b/Y differ` header (no embedded blob). `git
apply` cannot apply such hunks, so we strip them from the patch text and
recover the final file state directly from the repo's end-of-range git tag
(derived from `base.label` of the form `<start>..<end>`).
"""


def _filter_binary_patches(patch_content: str) -> str:
    """Remove binary diff sections from a git patch.

    Keeps text-only diff sections intact so `git apply` succeeds. Binary
    content is restored separately in fix-run.sh/test-run.sh by checking
    out the file at the end-of-range git tag.
    """
    if not patch_content:
        return patch_content

    lines = patch_content.split('\n')
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('diff --git'):
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith('diff --git'):
                if lines[i].startswith('GIT binary patch') or lines[i].startswith('Binary files'):
                    is_binary = True
                i += 1
            if not is_binary:
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    out = '\n'.join(result)
    if out and not out.endswith('\n'):
        out += '\n'
    return out


def _extract_binary_ops(patch_content: str) -> list:
    """Return list of (action, path) for binary diff sections in a patch.

    action is one of 'delete', 'add', 'modify'. path is the b/ path (new
    path) except for 'delete' where it's the a/ path.
    """
    if not patch_content:
        return []

    lines = patch_content.split('\n')
    ops = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].startswith('diff --git'):
            header = lines[i]
            section_start = i
            i += 1
            is_binary = False
            new_file = False
            deleted_file = False
            while i < n and not lines[i].startswith('diff --git'):
                ln = lines[i]
                if ln.startswith('GIT binary patch') or ln.startswith('Binary files'):
                    is_binary = True
                elif ln.startswith('new file mode'):
                    new_file = True
                elif ln.startswith('deleted file mode'):
                    deleted_file = True
                i += 1
            if is_binary:
                # Parse `diff --git a/foo b/bar` -> extract paths
                # Format: diff --git a/<path> b/<path>
                parts = header.split(' ')
                a_path = None
                b_path = None
                for p in parts:
                    if p.startswith('a/'):
                        a_path = p[2:]
                    elif p.startswith('b/'):
                        b_path = p[2:]
                if deleted_file:
                    if a_path:
                        ops.append(('delete', a_path))
                elif new_file:
                    if b_path:
                        ops.append(('add', b_path))
                else:
                    if b_path:
                        ops.append(('modify', b_path))
        else:
            i += 1
    return ops


def _extract_end_tag(base_label: str) -> str:
    """Given base.label `<start>..<end>`, return `<end>`. Empty string if not parseable."""
    if not base_label or '..' not in base_label:
        return ''
    return base_label.split('..', 1)[1]


def _binary_restore_shell(fix_patch: str, test_patch: str, end_tag: str) -> str:
    """Generate shell commands to restore binary files from end_tag.

    Combines both patches. Returns a bash snippet with no leading shebang.
    Uses `git show` to extract blobs and `git rm` for deletes.
    """
    if not end_tag:
        return '# no end_tag; skipping binary restore\n'
    ops_fix = _extract_binary_ops(fix_patch)
    ops_test = _extract_binary_ops(test_patch)
    # Dedup by path, prefer most-recent fix (overrides test-patch)
    merged = {}
    for action, path in ops_test + ops_fix:
        merged[path] = action
    if not merged:
        return '# no binary ops\n'

    lines = [
        '# --- Restore binary files from end-of-range tag ---',
        f'_END_REF="{end_tag}"',
        'if git rev-parse --verify -q "$_END_REF" >/dev/null; then',
        '    _HAVE_END=1',
        'else',
        '    git fetch --tags --quiet origin || true',
        '    if git rev-parse --verify -q "$_END_REF" >/dev/null; then _HAVE_END=1; else _HAVE_END=0; fi',
        'fi',
        'if [ "$_HAVE_END" = "1" ]; then',
    ]
    for path, action in merged.items():
        # Escape for shell single-quoting
        q = path.replace("'", "'\\''")
        if action == 'delete':
            lines.append(f"    rm -f '{q}' || true")
        else:
            # add or modify: extract blob into working tree
            lines.append(f"    mkdir -p \"$(dirname '{q}')\" 2>/dev/null || true")
            lines.append(f"    git show \"$_END_REF:{q}\" > '{q}' 2>/dev/null || true")
    lines.append('fi')
    lines.append('# --- end binary restore ---')
    return '\n'.join(lines) + '\n'
