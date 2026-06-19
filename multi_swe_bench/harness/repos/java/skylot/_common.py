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


def _binary_extract_shell(fix_patch: str, test_patch: str, end_tag: str) -> str:
    """Build-time snippet: copy every binary blob referenced by either patch from
    end_tag into /home/.bin_fixtures, BEFORE the hardening pass runs.

    The shared Image.dockerfile() hardening block deletes all tags, removes the
    origin remote and prunes unreachable objects, so the end-of-range tag (and
    its blobs) are gone by eval time. This snippet runs inside prepare.sh (part
    of extra_setup(), which executes before hardening) while the tag still
    exists, stashing the blobs outside the git tree so they survive. Runs from
    the repo root; emits a bash snippet with no leading shebang.
    """
    if not end_tag:
        return '# no end_tag; skipping binary pre-extract\n'
    merged = {}
    for action, path in _extract_binary_ops(test_patch) + _extract_binary_ops(fix_patch):
        merged[path] = action
    # Only adds/modifies need a blob; deletes are handled at restore time.
    adds = [p for p, a in merged.items() if a != 'delete']
    if not adds:
        return '# no binary blobs to pre-extract\n'

    lines = [
        '# --- Pre-extract binary fixtures from end-of-range tag (pre-hardening) ---',
        f'_END_REF="{end_tag}"',
        'if git rev-parse --verify -q "$_END_REF" >/dev/null 2>&1; then',
        '    _HAVE_END=1',
        'else',
        '    git fetch --tags --quiet origin >/dev/null 2>&1 || true',
        '    if git rev-parse --verify -q "$_END_REF" >/dev/null 2>&1; then _HAVE_END=1; else _HAVE_END=0; fi',
        'fi',
        'if [ "$_HAVE_END" = "1" ]; then',
    ]
    for path in adds:
        q = path.replace("'", "'\\''")
        lines.append(f"    mkdir -p \"/home/.bin_fixtures/$(dirname '{q}')\" 2>/dev/null || true")
        lines.append(f"    git show \"$_END_REF:{q}\" > '/home/.bin_fixtures/{q}' 2>/dev/null || true")
    lines.append('fi')
    lines.append('# --- end pre-extract ---')
    return '\n'.join(lines) + '\n'


def _binary_restore_shell(fix_patch: str, test_patch: str, end_tag: str) -> str:
    """Eval-time snippet: restore binary files from the fixtures pre-extracted to
    /home/.bin_fixtures by _binary_extract_shell.

    The fixtures were stashed at build time (before the hardening pass removed
    git history), so this no longer touches git at all: adds/modifies are copied
    in from /home/.bin_fixtures, deletes are `rm`-ed. Combines both patches
    (pass fix_patch='' for the test-only variant). Returns a bash snippet with
    no leading shebang; runs from the repo root.
    """
    if not end_tag:
        return '# no end_tag; skipping binary restore\n'
    merged = {}
    for action, path in _extract_binary_ops(test_patch) + _extract_binary_ops(fix_patch):
        merged[path] = action
    if not merged:
        return '# no binary ops\n'

    lines = ['# --- Restore binary files from pre-extracted fixtures ---']
    for path, action in merged.items():
        # Escape for shell single-quoting
        q = path.replace("'", "'\\''")
        if action == 'delete':
            lines.append(f"rm -f '{q}' || true")
        else:
            lines.append(
                f"if [ -f '/home/.bin_fixtures/{q}' ]; then "
                f"mkdir -p \"$(dirname '{q}')\" 2>/dev/null || true; "
                f"cp '/home/.bin_fixtures/{q}' '{q}'; fi"
            )
    lines.append('# --- end binary restore ---')
    return '\n'.join(lines) + '\n'
