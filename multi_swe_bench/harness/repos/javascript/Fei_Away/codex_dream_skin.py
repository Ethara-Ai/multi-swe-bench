import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# macos/package.json: "engines": {"node": ">=20"}; .github/workflows/ci.yml
# pins node-version "22". No dependencies, no lockfile: the repo uses only
# node: built-ins, so there is nothing for a package manager to install.
_NODE_IMAGE = "node:22.22.0-bookworm"

_ENV = """export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export NODE_ENV=test"""

# The repo's runner, macos/tests/run-tests.sh, is macOS-only (it requires
# Codex's bundled Node under /Applications and calls sips, plutil, launchctl,
# osascript). Its Node unit tests are plain assert scripts it invokes as
# `"$NODE" "$ROOT/tests/<name>.test.mjs"`; windows/tests/run-tests.ps1 does the
# same for windows/tests. Those scripts are platform-neutral, so the run stage
# executes every tracked *.test.mjs under macos/tests and windows/tests the same
# way, one result per file (a script aborts on its first failed assert).
_MARK = "codex-dream-skin-test"

_TESTS = f"""rc=0
mapfile -t test_files < <(git ls-files -co --exclude-standard -- 'macos/tests/*.test.mjs' 'windows/tests/*.test.mjs' | LC_ALL=C sort -u)
test "${{#test_files[@]}}" -gt 0
for test_file in "${{test_files[@]}}"; do
  echo "{_MARK} START $test_file"
  if timeout 600 node "$test_file"; then
    echo "{_MARK} PASS $test_file"
  else
    echo "{_MARK} FAIL $test_file"
    rc=1
  fi
done
exit "$rc\""""


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


class CodexDreamSkinImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        return (
            "ENV CI=true \\\n"
            "    NO_COLOR=1\n"
            "RUN git config --global --add safe.directory '*' && node --version"
        )

    # dockerfile() is the default Image.dockerfile(): clone, pin ${BASE_COMMIT},
    # extra_setup(), then Image._HARDENING_BLOCK. This is a per-PR base, so the
    # pin and the history scrub belong here.


class CodexDreamSkinImageDefault(Image):
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
        return CodexDreamSkinImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        apply = _apply_flags(self.pr)
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
# Nothing to install: macos/package.json declares no dependencies and the repo
# has no lockfile; every test imports node: built-ins and repo sources only.

# Section 3: gate
test "$(git rev-parse HEAD)" = "{sha}"
node -e 'const m = Number(process.versions.node.split(".")[0]); if (m < 20) {{ console.error("node " + process.version + " < 20 (engines.node)"); process.exit(1); }}'
mapfile -t test_files < <(git ls-files -- 'macos/tests/*.test.mjs' 'windows/tests/*.test.mjs')
test "${{#test_files[@]}}" -gt 0
for test_file in "${{test_files[@]}}"; do
  node --check "$test_file"
done
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
{_TESTS}
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
{_TESTS}
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
{_TESTS}
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


@Instance.register("Fei-Away", "Codex-Dream-Skin")
class CODEX_DREAM_SKIN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CodexDreamSkinImageDefault(self.pr, self._config)

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

        # Names are repo-relative paths, so the platform directory
        # (macos/tests vs windows/tests) keeps same-named files distinct.
        result_re = re.compile(
            rf"^{re.escape(_MARK)} (START|PASS|FAIL) (\S+\.test\.mjs)\s*$"
        )
        started: set[str] = set()
        for raw in log.splitlines():
            found = result_re.match(raw.rstrip("\r").strip())
            if not found:
                continue
            status, name = found.groups()
            if status == "START":
                started.add(name)
            elif status == "PASS":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

        # A script that started but printed no verdict (runner killed) failed.
        failed_tests |= started - passed_tests - failed_tests

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
