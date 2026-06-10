import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Map each Era 1 PR to its correct Bazel version.
# These base commits predate .bazelversion, so we determine the version
# from the closest git tag reachable from the base commit.
_BAZEL_VERSION_MAP = {
    13670: "4.1.0",
    13893: "4.2.0",
    14318: "4.2.1",
    14737: "5.0.0",
    15181: "5.1.0",
    15216: "5.1.1",
    15703: "5.2.0",
    16450: "5.3.1",
    16668: "5.3.2",
    17216: "6.0.0",
    17696: "6.1.0",
    17977: "5.4.0",  # Patch adds .bazelversion=5.4.0
    18039: "6.1.1",
    18503: "6.2.0",
    19050: "6.3.0",  # Patch adds .bazelversion=6.2.1
}


def _bazel_version_for_pr(pr_number: int) -> str:
    """Return the USE_BAZEL_VERSION for a given Era 1 PR number."""
    return _BAZEL_VERSION_MAP.get(pr_number, "6.3.0")


class BazelEra1ImageBase(Image):
    """Base image for Era 1 (PRs #13670-#19050, Bazel 4.x-6.x).

    These early releases predate .bazelversion, so we set
    USE_BAZEL_VERSION per-PR to prevent bazelisk from downloading
    the latest (incompatible) Bazel.

    JDK 11 is the correct runtime for this era.
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

    def dependency(self) -> Union[str, "Image"]:
        # Pin to jammy variant for GCC 11 (Ubuntu 22.04).
        # The default :11 tag now resolves to Ubuntu 26.04 with GCC 15,
        # which is too strict for Bazel 4.x/5.x C++ code.
        return "eclipse-temurin:11-jdk-jammy"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y \\
    git curl zip unzip python3 gcc g++ \\
    && rm -rf /var/lib/apt/lists/*

# Install bazelisk (auto-detects architecture)
RUN ARCH=$(uname -m) && \\
    if [ "$ARCH" = "aarch64" ]; then BAZEL_ARCH="arm64"; else BAZEL_ARCH="amd64"; fi && \\
    curl -fsSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-$BAZEL_ARCH" \\
    -o /usr/local/bin/bazel && chmod +x /usr/local/bin/bazel

{code}

{self.clear_env}

"""


class BazelEra1ImageDefault(Image):
    """Per-PR image for Era 1: checkout base commit, apply patches, pre-warm."""

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
        return BazelEra1ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        bazel_version = _bazel_version_for_pr(self.pr.number)
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

# Era 1 has no .bazelversion — set version explicitly
export USE_BAZEL_VERSION={version}

# Verify bazel version downloads correctly, skip heavy build (done at test time)
bazel version || true
""".format(pr=self.pr, version=bazel_version),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export USE_BAZEL_VERSION={version}
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
""".format(pr=self.pr, version=bazel_version),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export USE_BAZEL_VERSION={version}
git apply --whitespace=nowarn /home/test.patch
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
""".format(pr=self.pr, version=bazel_version),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
export USE_BAZEL_VERSION={version}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
""".format(pr=self.pr, version=bazel_version),
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


@Instance.register("bazelbuild", "13670_to_19050")
class BazelEra1(Instance):
    """Instance for Era 1 (PRs #13670-#19050).

    These commits predate .bazelversion. USE_BAZEL_VERSION is set per-PR
    based on the closest git tag reachable from each base commit, ensuring
    the correct Bazel version for each Bazel 4.x/5.x/6.x PR.
    The env var is set in run scripts (not via Docker ENV) so that patches
    which CREATE .bazelversion do not conflict.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    _APPLY_OPTS = "--whitespace=nowarn"

    @property
    def _bazel_version(self) -> str:
        return _bazel_version_for_pr(self.pr.number)

    @property
    def _BAZEL_TEST_CMD(self) -> str:
        return (
            f"export USE_BAZEL_VERSION={self._bazel_version} ; "
            "bazel --output_user_root=/tmp/bazel-output test //src/test/... "
            "--test_output=summary --keep_going --noshow_progress 2>&1 || true"
        )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelEra1ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return "bash -c 'cd /home/{repo} ; {cmd}'".format(
            repo=self.pr.repo,
            cmd=self._BAZEL_TEST_CMD,
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "{cmd}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                cmd=self._BAZEL_TEST_CMD,
            )
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git checkout -- . 2>/dev/null ; "
            "git apply {opts} /home/test.patch 2>/dev/null || "
            "git apply {opts} --3way /home/test.patch 2>/dev/null || true ; "
            "git apply {opts} /home/fix.patch 2>/dev/null || "
            "git apply {opts} --3way /home/fix.patch 2>/dev/null || true ; "
            "{cmd}"
            "'".format(
                repo=self.pr.repo,
                opts=self._APPLY_OPTS,
                cmd=self._BAZEL_TEST_CMD,
            )
        )

    def parse_log(self, test_log: str) -> TestResult:
        """Parse Bazel test output.

        Bazel test summary format:
            //path/to:TargetName  PASSED in 1.6s
            //path/to:TargetName  FAILED in 2.3s
            //path/to:TargetName  TIMEOUT in 300.0s
            //path/to:TargetName  FLAKY, failed in 1 out of 2 in 3.4s
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
        test_log = ansi_escape_pattern.sub("", test_log)

        re_passed = re.compile(r"^(//\S+)\s+PASSED\s+in\s+[\d.]+s", re.MULTILINE)
        re_failed = re.compile(r"^(//\S+)\s+FAILED\s+in\s+[\d.]+s", re.MULTILINE)
        re_timeout = re.compile(r"^(//\S+)\s+TIMEOUT\s+in\s+[\d.]+s", re.MULTILINE)
        re_flaky = re.compile(r"^(//\S+)\s+FLAKY", re.MULTILINE)
        re_no_status = re.compile(r"^(//\S+)\s+NO STATUS", re.MULTILINE)

        for match in re_passed.finditer(test_log):
            passed_tests.add(match.group(1))

        for match in re_failed.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_timeout.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_flaky.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_no_status.finditer(test_log):
            skipped_tests.add(match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
