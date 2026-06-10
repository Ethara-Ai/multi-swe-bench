import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class BazelEra3ImageBase(Image):
    """Base image for Era 3 (PRs #22381-#28951, Bazel 7.2.0-9.0.1).

    These releases use JDK 21 for compilation and runtime.
    Latest PRs also use remotejdk_25 (Bazel downloads JDK 25 at build time).
    JDK 21 on the host works for all.

    .bazelversion exists for all Era 3 commits.
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
        return "eclipse-temurin:21"

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


class BazelEra3ImageDefault(Image):
    """Per-PR image for Era 3: checkout base commit, apply patches, pre-warm."""

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
        return BazelEra3ImageBase(self.pr, self._config)

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

# .bazelversion exists for Era 3 — bazelisk selects the right version
bazel version || true
bazel build //src:bazel --noshow_progress 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bazel test //src/test/... --test_output=summary --keep_going --noshow_progress
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


@Instance.register("bazelbuild", "22381_to_28951")
class BazelEra3(Instance):
    """Instance for Era 3 (PRs #22381-#28951).

    These commits have .bazelversion. Bazelisk auto-downloads the
    correct Bazel version. JDK 21 is the host runtime, with
    remotejdk_XX handling JDK downloads for latest releases.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    _APPLY_OPTS = "--whitespace=nowarn"

    _BAZEL_TEST_CMD = (
        "bazel --output_user_root=/tmp/bazel-output test //src/test/... "
        "--test_output=summary --keep_going --noshow_progress 2>&1 || true"
    )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelEra3ImageDefault(self.pr, self._config)

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
