import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Shared vtparse UB fix script (applied AFTER patches to avoid context mismatch)
VTPARSE_FIX = """
# Fix vtparse UB (get_unchecked with out-of-bounds index aborts on modern Rust)
if [ -f "vtparse/src/lib.rs" ] && grep -q 'get_unchecked' vtparse/src/lib.rs; then
    python3 << 'VTFIX'
import re
with open('vtparse/src/lib.rs', 'r') as f:
    code = f.read()
# Pattern A: 2D unsafe TRANSITIONS access
code = re.sub(
    r'let v = unsafe \\{\\s*\\*?TRANSITIONS\\s*\\.get_unchecked\\(([^)]+)\\)\\s*\\.get_unchecked\\(([^)]+)\\)\\s*\\};?',
    r'let v = TRANSITIONS[\\1][\\2];',
    code, flags=re.DOTALL)
# Pattern B: 1D unsafe TRANSITIONS access
code = re.sub(
    r'unsafe\\s*\\{\\s*\\*?TRANSITIONS\\.get_unchecked\\(([^)]+)\\)\\s*\\};?',
    r'TRANSITIONS[\\1]',
    code, flags=re.DOTALL)
# Pattern C: unsafe EXIT/ENTRY access
code = re.sub(
    r'unsafe\\s*\\{\\s*\\*?EXIT\\.get_unchecked\\(([^)]+)\\)\\s*\\}',
    r'EXIT[\\1]',
    code, flags=re.DOTALL)
code = re.sub(
    r'unsafe\\s*\\{\\s*\\*?ENTRY\\.get_unchecked\\(([^)]+)\\)\\s*\\}',
    r'ENTRY[\\1]',
    code, flags=re.DOTALL)
with open('vtparse/src/lib.rs', 'w') as f:
    f.write(code)
VTFIX
fi
"""


class WeztermImageBase(Image):
    """Base image: rust:latest + cloned repo."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "rust:latest"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

RUN apt-get update && apt-get install -y cmake pkg-config libssl-dev python3

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class WeztermImageDefault(Image):
    """Per-PR image.

    Creates a separate test workspace at /home/test-workspace/ with symlinks
    to only the testable crates in the main repo. This avoids resolving
    problematic git deps (libssh-rs, mlua) while keeping the main repo
    checkout pristine for patch application.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return WeztermImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "rebuild-workspace.sh",
                """#!/bin/bash
# Rebuild test-workspace Cargo.toml after patches are applied.
# Extracts [workspace.dependencies] from patches, creates symlinks
# for all path dependencies (both workspace-level and crate-level),
# and ensures serde/derive feature is available when needed.

cd /home/{pr.repo}

python3 << 'PYSCRIPT'
import re, os, glob, subprocess

REPO = '/home/{pr.repo}'
WS = '/home/test-workspace'

workspace_deps = ""

# Source 1: Existing root Cargo.toml
if os.path.exists('Cargo.toml'):
    with open('Cargo.toml', 'r') as f:
        content = f.read()
    match = re.search(r'(\\[workspace\\.dependencies\\].*?)(?=\\n\\[(?!workspace\\.dependencies)|\\Z)', content, re.DOTALL)
    if match:
        workspace_deps = match.group(1).strip()

# Source 2: Extract from patch files (added lines in Cargo.toml diff)
if not workspace_deps:
    for patch_file in ['/home/fix.patch', '/home/test.patch']:
        if not os.path.exists(patch_file):
            continue
        with open(patch_file, 'r') as f:
            in_cargo_toml = False
            in_hunk = False
            added_lines = []
            for line in f:
                if line.startswith('diff --git'):
                    if in_cargo_toml and added_lines:
                        break
                    in_cargo_toml = (line.strip() == 'diff --git a/Cargo.toml b/Cargo.toml')
                    in_hunk = False
                    added_lines = []
                elif in_cargo_toml and line.startswith('@@'):
                    in_hunk = True
                elif in_cargo_toml and in_hunk:
                    if line.startswith('+') and not line.startswith('+++'):
                        added_lines.append(line[1:])
                    elif line.startswith('-') or line.startswith('\\\\\\\\'):
                        pass
                    else:
                        added_lines.append(line[1:] if line.startswith(' ') else line)
            if in_cargo_toml and added_lines:
                text = ''.join(added_lines)
                match = re.search(r'(\\[workspace\\.dependencies\\].*?)(?=\\n\\[(?!workspace\\.dependencies)|\\Z)', text, re.DOTALL)
                if match:
                    workspace_deps = match.group(1).strip()
                    break

# Build members list from directories
members = []
for d in ['term', 'termwiz', 'bidi', 'wezterm-dynamic', 'color-types',
          'filedescriptor', 'vtparse', 'strip-ansi-escapes']:
    if os.path.isdir(d):
        members.append(d)

def ensure_symlink(src_dir, target_path):
    \"\"\"Create symlink if source exists and target doesn't.\"\"\"
    if os.path.isdir(src_dir) and not os.path.exists(target_path):
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        os.symlink(src_dir, target_path)

# Extract path deps from workspace.dependencies and create symlinks
if workspace_deps:
    for m in re.finditer(r'path\\s*=\\s*"([^"]+)"', workspace_deps):
        p = m.group(1)
        ensure_symlink(REPO + '/' + p, WS + '/' + p)

# Scan member Cargo.tomls for path deps and create symlinks for those too
for member in members:
    cargo_path = member + '/Cargo.toml'
    if not os.path.exists(cargo_path):
        continue
    with open(cargo_path, 'r') as f:
        crate_content = f.read()
    for m in re.finditer(r'path\\s*=\\s*"([^"]+)"', crate_content):
        rel_path = m.group(1)
        # Resolve relative to crate directory
        abs_path = os.path.normpath(os.path.join(member, rel_path))
        # Create symlink in test-workspace at the same relative location
        target = WS + '/' + abs_path
        ensure_symlink(REPO + '/' + abs_path, target)

# Also scan newly-created symlinked dirs for THEIR path deps (one level deep)
for entry in os.listdir(WS):
    full = WS + '/' + entry
    if not os.path.islink(full) or entry in members:
        continue
    sub_cargo = os.path.join(full, 'Cargo.toml')
    if not os.path.exists(sub_cargo):
        continue
    with open(sub_cargo, 'r') as f:
        sub_content = f.read()
    for m in re.finditer(r'path\\s*=\\s*"([^"]+)"', sub_content):
        rel_path = m.group(1)
        abs_path = os.path.normpath(os.path.join(entry, rel_path))
        target = WS + '/' + abs_path
        ensure_symlink(REPO + '/' + abs_path, target)

# Ensure serde has 'derive' feature and is non-optional when crates use
# #[derive(Serialize/Deserialize)] directly (not behind feature gate)
for member in members:
    cargo_path = member + '/Cargo.toml'
    if not os.path.exists(cargo_path):
        continue
    # Check if source files use serde derive macros
    needs_derive = False
    for root, dirs, files in os.walk(member):
        for fname in files:
            if fname.endswith('.rs'):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r') as f:
                        src = f.read()
                    if 'derive(Serialize' in src or 'derive(Deserialize' in src or 'Serialize,' in src or 'Deserialize,' in src:
                        needs_derive = True
                        break
                except:
                    pass
        if needs_derive:
            break
    if not needs_derive:
        continue
    # Ensure serde dependency has derive feature AND is not optional
    with open(cargo_path, 'r') as f:
        toml_content = f.read()
    if 'serde' not in toml_content:
        continue
    new_content = toml_content
    # Add derive feature if missing
    new_content = re.sub(
        r'(serde\\s*=\\s*\\{{[^}}]*features\\s*=\\s*\\[)([^\\]]*)(\\])',
        lambda match: match.group(1) + match.group(2) + (', "derive"' if 'derive' not in match.group(2) else '') + match.group(3),
        new_content
    )
    # Case 2: serde = "1.0" (simple version) - convert to table with derive + optional
    if new_content == toml_content and re.search(r'serde\\s*=\\s*"[^"]+"', toml_content):
        new_content = re.sub(
            r'serde\\s*=\\s*"([^"]+)"',
            r'serde = {{version = "\\1", features = ["derive"], optional = true}}',
            new_content
        )
    if new_content != toml_content:
        with open(cargo_path, 'w') as f:
            f.write(new_content)
    # Enable use_serde as default feature so serde is always active
    if 'use_serde' in toml_content:
        with open(cargo_path, 'r') as f:
            updated = f.read()
        if '[features]' in updated and 'default' in updated:
            updated = re.sub(r'(default\\s*=\\s*\\[)([^\\]]*)(\\])',
                lambda m: m.group(1) + m.group(2) + (', "use_serde"' if 'use_serde' not in m.group(2) else '') + m.group(3),
                updated)
        elif '[features]' in updated:
            updated = updated.replace('[features]', '[features]\\ndefault = ["use_serde"]')
        if updated != open(cargo_path).read():
            with open(cargo_path, 'w') as f:
                f.write(updated)

# Auto-detect missing common crate deps (cfg-if, bitflags) in member Cargo.tomls
COMMON_CRATES = {{
    'cfg_if': 'cfg-if = "1"',
    'bitflags': 'bitflags = "2"',
}}
for member in members:
    cargo_path = member + '/Cargo.toml'
    if not os.path.exists(cargo_path):
        continue
    with open(cargo_path, 'r') as f:
        toml_content = f.read()
    added = []
    for crate_use, dep_line in COMMON_CRATES.items():
        crate_name = dep_line.split('=')[0].strip()
        if crate_name in toml_content:
            continue
        # Check if source uses this crate
        for root, dirs, files in os.walk(member):
            for fname in files:
                if fname.endswith('.rs'):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r') as f:
                            src = f.read()
                        if crate_use + '::' in src or crate_use + '!' in src:
                            added.append(dep_line)
                            break
                    except:
                        pass
            else:
                continue
            break
    if added:
        # Insert after [dependencies] line
        if '[dependencies]' in toml_content:
            toml_content = toml_content.replace('[dependencies]', '[dependencies]\\n' + '\\n'.join(added))
        else:
            toml_content += '\\n[dependencies]\\n' + '\\n'.join(added) + '\\n'
        with open(cargo_path, 'w') as f:
            f.write(toml_content)

# Write workspace Cargo.toml
members_str = ','.join('"' + m + '"' for m in members)
toml = '[workspace]\\nmembers = [' + members_str + ']\\nresolver = "2"\\n'
if workspace_deps:
    toml += '\\n' + workspace_deps + '\\n'

with open(WS + '/Cargo.toml', 'w') as f:
    f.write(toml)
PYSCRIPT

# Ensure all member symlinks exist
for dir in term termwiz bidi wezterm-dynamic color-types filedescriptor vtparse strip-ansi-escapes; do
    if [ -d "/home/{pr.repo}/$dir" ] && [ ! -e "/home/test-workspace/$dir" ]; then
        ln -sf /home/{pr.repo}/$dir /home/test-workspace/$dir
    fi
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
git submodule update --init --recursive

# Detect testable crate name (term vs wezterm-term)
TERM_CRATE="wezterm-term"
if [ -d "term" ]; then
    TERM_CRATE=$(grep '^name' term/Cargo.toml | head -1 | sed 's/.*= *"\\(.*\\)"/\\1/')
fi

# Build workspace members list from available crates
MEMBERS=""
for dir in term termwiz bidi wezterm-dynamic color-types filedescriptor vtparse strip-ansi-escapes; do
    if [ -d "$dir" ]; then
        MEMBERS="$MEMBERS\\"$dir\\","
    fi
done

# Create separate test workspace with symlinks
mkdir -p /home/test-workspace
cat > /home/test-workspace/Cargo.toml << TOML
[workspace]
members = [$MEMBERS]
resolver = "2"
TOML

for dir in term termwiz bidi wezterm-dynamic color-types filedescriptor vtparse strip-ansi-escapes; do
    if [ -d "/home/{pr.repo}/$dir" ]; then
        ln -sf /home/{pr.repo}/$dir /home/test-workspace/$dir
    fi
done

# Warm compilation cache
cd /home/test-workspace
cargo test --no-fail-fast -p $TERM_CRATE || true
if [ -d "/home/{pr.repo}/termwiz" ]; then
    cargo test --no-fail-fast -p termwiz || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{vtparse_fix}

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply test patch (--reject handles binary diffs gracefully)
git apply --reject /home/test.patch || true

# Fail if any text patches were rejected
REJ_COUNT=$(find . -name '*.rej' | wc -l)
if [ "$REJ_COUNT" -gt 0 ]; then
    echo "ERROR: $REJ_COUNT reject file(s) found:"
    find . -name '*.rej'
    exit 1
fi

# Restore binary files that git apply --reject skips
python3 << 'BINFIX'
import re, os, subprocess
for patch_file in ['/home/test.patch']:
    if not os.path.exists(patch_file):
        continue
    with open(patch_file, 'r', errors='replace') as f:
        content = f.read()
    # Find binary new file diffs and extract target blob SHA
    for m in re.finditer(r'diff --git a/(.+?) b/\\1\\nnew file mode \\d+\\nindex [0-9a-f]+\\.\\.([0-9a-f]+)\\nBinary files /dev/null and b/\\1 differ', content):
        filepath = m.group(1)
        blob_sha = m.group(2)
        if os.path.exists(filepath):
            continue
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
        try:
            data = subprocess.check_output(['git', 'cat-file', 'blob', blob_sha])
            with open(filepath, 'wb') as f:
                f.write(data)
            print(f'Restored binary: {{filepath}} (blob {{blob_sha}})')
        except subprocess.CalledProcessError:
            print(f'Warning: could not restore binary {{filepath}} (blob {{blob_sha}} not found)')
BINFIX

{vtparse_fix}

# Rebuild test workspace (extracts workspace.dependencies from patches)
bash /home/rebuild-workspace.sh

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Apply test + fix patches (--reject handles binary diffs gracefully)
git apply --reject /home/test.patch /home/fix.patch || true

# Fail if any text patches were rejected
REJ_COUNT=$(find . -name '*.rej' | wc -l)
if [ "$REJ_COUNT" -gt 0 ]; then
    echo "ERROR: $REJ_COUNT reject file(s) found:"
    find . -name '*.rej'
    exit 1
fi

# Restore binary files that git apply --reject skips
# Parses patch for "Binary files /dev/null and b/PATH differ" with index line
python3 << 'BINFIX'
import re, os, subprocess
for patch_file in ['/home/test.patch', '/home/fix.patch']:
    if not os.path.exists(patch_file):
        continue
    with open(patch_file, 'r', errors='replace') as f:
        content = f.read()
    # Find binary new file diffs and extract target blob SHA
    for m in re.finditer(r'diff --git a/(.+?) b/\\1\\nnew file mode \\d+\\nindex [0-9a-f]+\\.\\.([0-9a-f]+)\\nBinary files /dev/null and b/\\1 differ', content):
        filepath = m.group(1)
        blob_sha = m.group(2)
        if os.path.exists(filepath):
            continue
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else '.', exist_ok=True)
        try:
            data = subprocess.check_output(['git', 'cat-file', 'blob', blob_sha])
            with open(filepath, 'wb') as f:
                f.write(data)
            print(f'Restored binary: {{filepath}} (blob {{blob_sha}})')
        except subprocess.CalledProcessError:
            print(f'Warning: could not restore binary {{filepath}} (blob {{blob_sha}} not found)')
BINFIX

{vtparse_fix}

# Rebuild test workspace (extracts workspace.dependencies from patches)
bash /home/rebuild-workspace.sh

cd /home/test-workspace
cargo test --no-fail-fast

""".format(pr=self.pr, vtparse_fix=VTPARSE_FIX),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("wezterm", "wezterm")
class Wezterm(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WeztermImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"test (\S+) ... ok")]
        re_fail_tests = [re.compile(r"test (\S+) ... FAILED")]
        re_skip_tests = [re.compile(r"test (\S+) ... ignored")]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
