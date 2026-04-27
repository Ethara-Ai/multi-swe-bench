import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# execa: sindresorhus/execa
# ---------------------------------------------------------------------------
# Process execution for humans.  Tests use AVA across every version in the
# dataset.  We run tests with `npx ava --tap` to get machine-parseable TAP
# output regardless of the AVA major version.
#
# 34 PRs in dataset, range #73 – #1206.
# Single node:20 base image works for all PRs.
#
# Image chain:  node:20 → Base (clone repo)
#                        → Per-PR (git checkout + npm install + patches + run)
#
# Version-specific handling:
#   1. PRs 73-158 (v0.6-v1.0): package.json has "ava": "*", which resolves
#      to AVA 7 (ESM-only, incompatible).  prepare.sh overrides with
#      ava@0.25 (last CJS-compatible 0.x release that works on node:20).
#   2. PRs 312+ (v2.0+): AVA is pinned (^2.1+), resolves correctly.
#
# TAP output format (all AVA versions with --tap):
#   TAP version 13
#   ok 1 - file › test name
#   not ok 2 - file › test name
#   ...
#   1..N
#   # tests N
#   # pass N
#   # fail N
# ---------------------------------------------------------------------------

# PRs at or below this number have "ava": "*" and need an override.
_AVA_WILDCARD_CUTOFF = 158


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class ExecaImageBase(Image):
    """Base image — node:20, clone repo."""

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
        return "node:20"

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
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo,
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y git curl

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class ExecaImageDefault(Image):
    """Per-PR instance image.  Checks out the base commit, installs deps,
    and injects patches + run scripts."""

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
        return ExecaImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _ava_override(self) -> str:
        """Early PRs have ``"ava": "*"`` which resolves to AVA 7 (ESM-only).
        Override with ava@0.25 — last 0.x that works on Node 20."""
        if self.pr.number <= _AVA_WILDCARD_CUTOFF:
            return "npm install ava@0.25 --save-dev"
        return ""

    def _make_run_script(self, patches: str) -> str:
        """Generate a run script with optional patch application."""
        return """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{patches}
npx ava --tap 2>&1
""".format(
            repo=self.pr.repo,
            patches=patches,
        )

    def files(self) -> list[File]:
        ava_override = self._ava_override()
        install_cmd = "npm install || true"
        if ava_override:
            install_cmd = "npm install || true\n{ava}".format(ava=ava_override)

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
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

{install}

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    install=install_cmd,
                ),
            ),
            # run.sh — baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh — test.patch only
            File(
                ".",
                "test-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' /home/test.patch || "
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' --3way /home/test.patch || true"
                ),
            ),
            # fix-run.sh — test.patch + fix.patch
            File(
                ".",
                "fix-run.sh",
                self._make_run_script(
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' /home/test.patch /home/fix.patch || "
                    "git apply --whitespace=nowarn --exclude='*.png' --exclude='*.sketch' --3way /home/test.patch /home/fix.patch || true"
                ),
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


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("sindresorhus", "execa")
class Execa(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExecaImageDefault(self.pr, self._config)

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
        """Parse AVA TAP output.

        TAP format:
            TAP version 13
            ok 1 - file › test name
            not ok 2 - file › test name
            ...
            1..N
            # tests N
            # pass N
            # fail N

        We extract individual test results from ``ok``/``not ok`` lines.
        The test name is everything after the number and optional dash.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        # TAP result lines
        re_ok = re.compile(r"^ok\s+\d+\s+-?\s*(.+)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s+-?\s*(.+)$")
        # TAP skip directive: "ok N - name # skip reason"
        re_skip = re.compile(r"#\s*skip", re.IGNORECASE)

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            m_ok = re_ok.match(stripped)
            if m_ok:
                name = m_ok.group(1).strip()
                if re_skip.search(stripped):
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)
                continue

            m_fail = re_not_ok.match(stripped)
            if m_fail:
                name = m_fail.group(1).strip()
                failed_tests.add(name)
                continue

        # Ensure no overlap
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
