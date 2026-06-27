"""PM2 harness for Era 2 (PRs 3720-5971): mocha ^5+, test/unit.sh.

Uses ubuntu:latest with Node.js from apt (v18).
"""

from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "3720_to_99999"


class _ImageBase(Image):
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
        return "ubuntu:latest"

    def image_tag(self) -> str:
        # Single shared toolchain base, reused by every PR and era. It is
        # commit-agnostic (no clone/checkout/hardening), so it is shareable.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # Node.js (v18), npm and bc from Ubuntu apt; the rest of the build
        # tooling is provided by Image.default_packages.
        return ["nodejs", "npm", "bc"]

    def dockerfile(self) -> str:
        # Toolchain ONLY -- deliberately no git clone/checkout/hardening here so
        # the image stays commit-agnostic and shareable. The per-PR clone,
        # checkout of BASE_COMMIT and git hardening live in _ImageDefault.
        base_img = self.dependency()
        default_packages = [
            "ca-certificates", "curl", "build-essential", "git", "gnupg",
            "make", "python3", "sudo", "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "ENV LANG=C.UTF-8\n"
            "ENV LC_ALL=C.UTF-8"
        )
        sections.append(apt_command)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class _ImageDefault(Image):
    """Era 2 image: runs test/unit.sh with mocha ^5+."""

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
        return _ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Per-PR exclusions of flaky, fix-unrelated test files. These files contain
    # timing-dependent tests that flake (pass in the test stage, fail in the fix
    # stage) and would otherwise invalidate a genuinely-resolved instance. The
    # excluded files do NOT contain the PR's f2p tests.
    _FLAKY_EXCLUDE = {
        4224: {"test/programmatic/signals.js"},  # 'should stop script after 3000ms' timing flake; f2p live in dump/resurect
    }

    # Bash/e2e orchestrator scripts that run *other* tests -- never run directly.
    _SH_RUNNERS = {"e2e.sh", "unit.sh"}

    def _runnable_test_files(self) -> list[str]:
        """Test files touched by the test patch that the run scripts execute.
        Prefers mocha .js specs; if the patch adds no runnable .js, falls back to
        .sh e2e/bash tests so e2e-only PRs still exercise their f2p (instances
        that have .js are unchanged). Excludes fixtures, master runners (e2e.sh,
        unit.sh), pm2_* helpers and include.sh."""
        exclude = self._FLAKY_EXCLUDE.get(self.pr.number, set())
        seen: set[str] = set()
        js: list[str] = []
        sh: list[str] = []
        for path in re.findall(r"^\+\+\+ b/(.+?)\s*$", self.pr.test_patch or "", re.M):
            if path == "/dev/null" or not path.startswith("test/") or path in exclude:
                continue
            if "/fixtures/" in path or path in seen:
                continue
            name = path.rsplit("/", 1)[-1]
            if path.endswith(".js"):
                seen.add(path)
                js.append(path)
            elif (
                path.endswith(".sh")
                and name not in self._SH_RUNNERS
                and not name.startswith("pm2_")
                and name != "include.sh"
            ):
                seen.add(path)
                sh.append(path)
        return js if js else sh

    def files(self) -> list[File]:
        test_files = " ".join(self._runnable_test_files())
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

""",
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
bash /home/check_git_changes.sh

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{repo}
export PATH="$PATH:/home/{repo}/node_modules/.bin"
npm install 2>&1 || true
TEST_FILES="{tf}"
for f in $TEST_FILES; do
  [ -f "$f" ] || continue
  echo "[~] Running $f"
  case "$f" in
    *.sh) NODE_ENV=test timeout 300 bash "$f" 2>&1 || true ;;
    *)    NODE_ENV=test timeout 300 mocha --exit --retries 2 --timeout 60000 "$f" 2>&1 || true ;;
  esac
done

""".format(repo=self.pr.repo, tf=test_files),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{repo}
export PATH="$PATH:/home/{repo}/node_modules/.bin"
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
npm install 2>&1 || true
TEST_FILES="{tf}"
for f in $TEST_FILES; do
  [ -f "$f" ] || continue
  echo "[~] Running $f"
  case "$f" in
    *.sh) NODE_ENV=test timeout 300 bash "$f" 2>&1 || true ;;
    *)    NODE_ENV=test timeout 300 mocha --exit --retries 2 --timeout 60000 "$f" 2>&1 || true ;;
  esac
done

""".format(repo=self.pr.repo, tf=test_files),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{repo}
export PATH="$PATH:/home/{repo}/node_modules/.bin"
git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
npm install 2>&1 || true
TEST_FILES="{tf}"
for f in $TEST_FILES; do
  [ -f "$f" ] || continue
  echo "[~] Running $f"
  case "$f" in
    *.sh) NODE_ENV=test timeout 300 bash "$f" 2>&1 || true ;;
    *)    NODE_ENV=test timeout 300 mocha --exit --retries 2 --timeout 60000 "$f" 2>&1 || true ;;
  esac
done

""".format(repo=self.pr.repo, tf=test_files),
            ),
        ]

    def dockerfile(self) -> str:
        # _ImageBase is a shared toolchain image with NO repository, so the
        # per-PR clone, checkout of BASE_COMMIT and git hardening happen here.
        # This image has an Image dependency, so DockerfileEnhancer leaves it
        # raw (no auto-hardening / no REPO_URL,BASE_COMMIT build args) -- hence
        # everything is baked in explicitly below.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        repo_url = f"https://github.com/{self.pr.org}/{repo}.git"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG REPO_URL="{repo_url}"
ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


def _parse_mocha_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI escape codes
    ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

    # Mocha spec reporter patterns (era 2 uses ✔):
    #   ✔ test name
    #   ✔ test name (282ms)
    #   ✓ test name             (both accepted)
    re_pass = re.compile(r"^[✔✓]\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?$")
    re_fail = re.compile(r"^\d+\)\s+(.+?)(?:\s+\(\d+(?:ms|s)\))?$")

    # unit.sh success marker: [V] test/programmatic/foo.mocha.js succeeded
    re_unit_pass = re.compile(r"^\[V\]\s+(\S+)\s+succeeded")
    # unit.sh failure marker: ######## TEST ✘ test/... FAILED
    re_unit_fail = re.compile(r"^#{4,}\s+TEST\s+[✘]\s+(\S+)\s+FAILED")

    # e2e/bash harness markers (test/e2e/*.sh, test/bash/*.sh via include.sh):
    #   ------------> ✔ <name>   (success / spec)
    #   ######## ✘ <name>        (fail)
    re_e2e_pass = re.compile(r"^-+>\s*[✔✓]\s+(.+)$")
    re_e2e_fail = re.compile(r"^#{4,}\s*[✘]\s+(.+)$")

    for line in test_log.splitlines():
        clean = ansi_re.sub("", line).strip()
        if not clean:
            continue

        # Skip PM2 daemon log lines
        if re.match(r"^\[\d{4}-\d{2}-\d{2}", clean):
            continue

        # Check mocha spec pass/fail
        pass_match = re_pass.match(clean)
        if pass_match:
            passed_tests.add(pass_match.group(1).strip())
            continue

        fail_match = re_fail.match(clean)
        if fail_match:
            test_name = fail_match.group(1).strip()
            if test_name and not test_name.endswith(":"):
                failed_tests.add(test_name)
            continue

        # Check unit.sh level markers
        unit_pass_match = re_unit_pass.match(clean)
        if unit_pass_match:
            passed_tests.add(unit_pass_match.group(1))
            continue

        unit_fail_match = re_unit_fail.match(clean)
        if unit_fail_match:
            failed_tests.add(unit_fail_match.group(1))
            continue

        # e2e/bash harness markers
        e2e_pass_match = re_e2e_pass.match(clean)
        if e2e_pass_match:
            passed_tests.add(e2e_pass_match.group(1).strip())
            continue

        e2e_fail_match = re_e2e_fail.match(clean)
        if e2e_fail_match:
            failed_tests.add(e2e_fail_match.group(1).strip())
            continue

    # PM2 specs reuse the same `it()` name across describe blocks, so one name
    # can be reported as both passed and failed in a single run. TestResult
    # requires the three sets to be disjoint, so collapse overlaps with failed
    # taking precedence.
    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("Unitech", "pm2_3720_to_99999")
class PM2_3720_TO_99999(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageDefault(self.pr, self._config)

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
        return _parse_mocha_log(test_log)


# Route the dash-joined number_interval (canonical prs_in_bundle format) of the
# release-bundled resolved_genuine dataset to the PM2_3720_TO_99999 config. Each record
# in this era carries a unique number_interval == sorted PR/issue numbers from its
# resolved_issues joined by "-". Instance.create() looks up
# f"{org}/{number_interval}"; Instance.register returns the class unchanged, so it
# answers to every key (the "pm2_3720_to_99999" era key above is kept for back-compat).
_BUNDLE_NUMBER_INTERVALS = [
    "1234-3412-3415-3483-3720-3786-3807-3823-3831-3844-3865-3869-3878-3883",
    "1349-3081-3732-3940-4021-4150-4203-4210",
    "4018-4032",
    "2097-4224-4239-4297-4306-4367-4378-4391-4436",
    "3347-3884-4058-4254-4271-4280-4288-4300-4364-4372",
    "3471-3555-3651-3691-4013-4431-4474-4480-4485",
    "1234-4404-4517-4560-4589-4595-4614-4629-4639-4652",
    "4892-4897",
    "5208-5330-5443-5451-5574-5658-5672",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("Unitech", _ni)(PM2_3720_TO_99999)
