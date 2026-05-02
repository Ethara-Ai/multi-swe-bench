
import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "node:10"

    def image_tag(self) -> str:
        return "base-gulp3"

    def workdir(self) -> str:
        return "base-gulp3"

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

{self.global_env}

WORKDIR /home/
# node:10 is Debian Stretch (EOL) - point apt to archive
RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list && \
    apt update && apt install -y git python3 jq

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

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
                "strip_binary_diffs.py",
                """\
#!/usr/bin/env python3
\"\"\"Strip binary diffs from patch files.

Removes binary diff sections (which git apply cannot handle when index
hashes are short) and writes the paths of binary files to a companion
file so they can be restored from the merge commit.

Usage:
    python3 strip_binary_diffs.py <patch1> [<patch2> ...]

For each <patchN>, writes <patchN>.binfiles with one binary path per line.
\"\"\"
import re
import sys

def strip_binary_diffs(patch_path):
    with open(patch_path, 'r', errors='replace') as f:
        content = f.read()

    # Split into per-file diffs
    # Manual split on 'diff --git' boundaries (Python 3.5 compat - no lookahead in re.split)
    diffs = []
    current = []
    for line in content.splitlines(True):
        if line.startswith('diff --git ') and current:
            diffs.append(''.join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        diffs.append(''.join(current))
    text_diffs = []
    binary_paths = []
    for diff in diffs:
        if not diff.strip():
            continue
        # Check for binary markers
        if 'Binary files' in diff or 'GIT binary patch' in diff:
            # Extract the b/ path from "diff --git a/... b/..."
            m = re.match(r'diff --git a/.+ b/(.+)', diff)
            if m:
                binary_paths.append(m.group(1).strip())
            continue
        text_diffs.append(diff)

    with open(patch_path, 'w') as f:
        f.write('\\n'.join(text_diffs))

    # Write binary file paths to companion file
    with open(patch_path + '.binfiles', 'w') as f:
        for p in binary_paths:
            f.write(p + '\\n')

if __name__ == '__main__':
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
""",
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
                "prepare.sh",
                """#!/bin/bash
set -e
export PATH=./node_modules/.bin:$PATH
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

npm install --ignore-scripts || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export PATH=./node_modules/.bin:$PATH

cd /home/{pr.repo}
npx gulp lib || true
npx gulp unittestcli

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
export PATH=./node_modules/.bin:$PATH
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

cd /home/{pr.repo}

# Strip binary diffs that git apply cannot handle (short index hashes)
python3 /home/strip_binary_diffs.py /home/test.patch

# Apply text-only diffs (don't abort on failure — some hunks may fail on lock files)
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true

# Restore binary files from the merge commit
if [ -s /home/test.patch.binfiles ]; then
    FIRST_BIN=$(head -1 /home/test.patch.binfiles)
    MERGE_SHA=$(git log --all --diff-filter=A --format='%H' -- "$FIRST_BIN" | head -1)
    if [ -n "$MERGE_SHA" ]; then
        while IFS= read -r binpath; do
            [ -z "$binpath" ] && continue
            git checkout "$MERGE_SHA" -- "$binpath" 2>/dev/null || true
        done < /home/test.patch.binfiles
    fi
fi

npm install --ignore-scripts || true

# Re-enable strict mode for test execution
set -e
npx gulp lib || true
npx gulp unittestcli

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
export PATH=./node_modules/.bin:$PATH
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

cd /home/{pr.repo}

# Strip binary diffs from both patches
python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch

# Apply text-only diffs (don't abort on failure — some hunks may fail on lock files)
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch 2>/dev/null || true

# Restore binary files from the merge commit
for patchfile in /home/test.patch /home/fix.patch; do
    binlist="${{patchfile}}.binfiles"
    if [ -s "$binlist" ]; then
        FIRST_BIN=$(head -1 "$binlist")
        MERGE_SHA=$(git log --all --diff-filter=A --format='%H' -- "$FIRST_BIN" | head -1)
        if [ -n "$MERGE_SHA" ]; then
            while IFS= read -r binpath; do
                [ -z "$binpath" ] && continue
                git checkout "$MERGE_SHA" -- "$binpath" 2>/dev/null || true
            done < "$binlist"
        fi
    fi
done

npm install --ignore-scripts || true

# Re-enable strict mode for test execution
set -e
npx gulp lib || true
npx gulp unittestcli

""".format(pr=self.pr),
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
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("mozilla", "pdf_js_0_to_9999")
class PdfJsGulp3(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._seen_failed_names = set()

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        # Jasmine summary: "327 specs, 0 failures" or "1088 specs, 0 failures, 56 pending specs"
        re_summary = re.compile(
            r"(\d+)\s+specs?,\s+(\d+)\s+failures?(?:,\s+(\d+)\s+pending)?"
        )

        total_specs = 0
        total_failures = 0
        total_pending = 0

        for line in clean_log.splitlines():
            line = line.strip()
            m = re_summary.search(line)
            if m:
                total_specs = int(m.group(1))
                total_failures = int(m.group(2))
                total_pending = int(m.group(3) or 0)

        total_passed = total_specs - total_failures - total_pending

        # Extract individual failure names from jasmine "Failures:" section only.
        # Must NOT match the "Pending:" section which uses the same numbered format.
        failures_section_re = re.compile(
            r"Failures:\s*\n(.*?)(?=\nPending:|\n\d+\s+specs?)", re.DOTALL
        )
        failures_match = failures_section_re.search(clean_log)
        if failures_match:
            failures_text = failures_match.group(1)
            numbered_fail_re = re.compile(
                r"^\s*\d+\)\s+(.+?)\s*$", re.MULTILINE
            )
            for m in numbered_fail_re.finditer(failures_text):
                test_name = m.group(1).strip()
                if test_name and not test_name.startswith("Message:"):
                    failed_tests.add(test_name)

        if total_passed > 0:
            passed_tests.add("ToTal_Test")

        if not failed_tests and total_failures > 0:
            failed_tests.add("ToTal_Test")

        if total_pending > 0 and not skipped_tests:
            skipped_tests.add("ToTal_Pending")

        skipped_tests -= passed_tests | failed_tests

        self._seen_failed_names |= failed_tests
        if total_passed > 0 and total_failures == 0 and self._seen_failed_names:
            # All previously-failed tests now pass (zero failures)
            passed_tests |= self._seen_failed_names
            passed_tests.discard("ToTal_Test") if "ToTal_Test" not in self._seen_failed_names else None
            passed_tests.add("ToTal_Test")
        elif total_passed > 0 and total_failures > 0 and self._seen_failed_names:
            # Partial recovery: some previously-failed tests no longer fail
            recovered = self._seen_failed_names - failed_tests
            recovered.discard("ToTal_Test")
            if recovered:
                passed_tests |= recovered

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
