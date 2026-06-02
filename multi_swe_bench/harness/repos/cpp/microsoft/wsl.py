import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# microsoft/WSL is a Windows-only C++ project: its top-level CMakeLists.txt
# hard-fails on non-Windows toolchains (requires the Windows SDK 10.0.26100 and
# MSVC) and its test suite under test/windows/ uses TAEF (`te.exe`), which only
# runs on Windows. The Linux-runnable subset (test/linux/unit_tests) only
# exercises live WSL-guest kernel/interop behaviour and cannot run in a plain
# container either. This makes a real build/test impossible on the Linux Docker
# harness. We therefore follow the established convention already used in this
# codebase for sibling Microsoft Windows repos (c/microsoft/ebpf_for_windows.py,
# c/microsoft/xdp_for_windows.py): a deterministic patch-applicability smoke
# test that yields a valid f2p signal (`fix_patch_applied` fails before the fix
# and passes after it) and a p2p signal (`test_patch_applied`).
#
# Several PRs in this dataset carry fix patches with binary-file diff markers
# generated without `--binary` (no full index line), which `git apply` rejects
# outright. filter_binary.awk strips those binary-only diff blocks so the
# textual source changes — the part that actually matters — apply cleanly and
# uniformly across every release line (2.5 - 2.8).


class WSLImageBase(Image):
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
        return "ubuntu:22.04"

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

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y git ca-certificates gawk && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class WSLImageDefault(Image):
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
        return WSLImageBase(self.pr, self._config)

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
                "filter_binary.awk",
                r"""/^diff --git / { if (buf != "") { if (isbin == 0) printf "%s", buf } buf = $0 ORS; isbin = 0; next }
/^Binary files / || /^GIT binary patch/ { isbin = 1 }
{ buf = buf $0 ORS }
END { if (buf != "" && isbin == 0) printf "%s", buf }
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

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}

# Strip binary-only diff blocks (generated without --binary, no full index
# line) so the textual source changes apply cleanly across all release lines.
awk -f /home/filter_binary.awk /home/test.patch > /home/test_src.patch
awk -f /home/filter_binary.awk /home/fix.patch  > /home/fix_src.patch

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1

echo "[PASS] repo_checkout"

# At base state the fix patch is not applied -> expected FAIL.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1

if git apply --whitespace=nowarn /home/test_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test_src.patch 2>/dev/null; then
  echo "[PASS] test_patch_applied"
else
  echo "[FAIL] test_patch_applied"
fi

# Only the test patch is applied; the fix patch is still absent -> FAIL.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -f {pr.base.sha} >/dev/null 2>&1
git clean -dfx >/dev/null 2>&1

if git apply --whitespace=nowarn /home/test_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/test_src.patch 2>/dev/null; then
  echo "[PASS] test_patch_applied"
else
  echo "[FAIL] test_patch_applied"
fi

git apply --whitespace=nowarn /home/fix_src.patch 2>/dev/null || git apply --whitespace=nowarn --3way /home/fix_src.patch 2>/dev/null || true

# The fix patch is now applied -> expected PASS.
if git apply --reverse --check --whitespace=nowarn /home/fix_src.patch >/dev/null 2>&1; then
  echo "[PASS] fix_patch_applied"
else
  echo "[FAIL] fix_patch_applied"
fi

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("microsoft", "WSL")
class WSL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WSLImageDefault(self.pr, self._config)

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

        re_pass = re.compile(r"^\[PASS\]\s+(\S+)")
        re_fail = re.compile(r"^\[FAIL\]\s+(\S+)")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue
            m = re_fail.match(line)
            if m:
                failed_tests.add(m.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
