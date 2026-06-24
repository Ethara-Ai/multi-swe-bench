import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.bazelbuild.bazel_targets import (
    extract_test_targets,
)


class BazelEra2ImageBase(Image):
    """Base image for Era 2 (PRs #19167-#21889, Bazel 6.3.1-7.1.1).

    These releases have .bazelversion and use JDK 11 as the runtime.
    Bazelisk reads .bazelversion to download the correct Bazel.
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
        # The default :11 tag now resolves to Ubuntu 26.04 with GCC 15.
        return "eclipse-temurin:11-jdk-jammy"

    def image_tag(self) -> str:
        return "base-era2"

    def workdir(self) -> str:
        return "base-era2"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # mam: SINGLE shared base per era (NOT per-PR). Clone full history ONCE so
        # every PR in this era can checkout its own base.sha. Base carries LIGHT
        # hardening (network lockdown); the per-PR layer carries the FULL gc-prune
        # hardening. "# syntax" opts out of DockerfileEnhancer auto-injection.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    TZ=UTC \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl zip unzip python3 gcc g++ ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Install bazelisk (auto-detects architecture)
RUN ARCH=$(uname -m) && \\
    if [ "$ARCH" = "aarch64" ]; then BAZEL_ARCH="arm64"; else BAZEL_ARCH="amd64"; fi && \\
    curl -fsSL "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-$BAZEL_ARCH" \\
    -o /usr/local/bin/bazel && chmod +x /usr/local/bin/bazel

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

# BASE light hardening: keep FULL history (per-PR layer checks out base.sha) but
# remove the remote so the model can never fetch/pull the fix from upstream.
WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class BazelEra2ImageDefault(Image):
    """Per-PR image for Era 2: checkout base commit, apply patches, pre-warm."""

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
        return BazelEra2ImageBase(self.pr, self._config)

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

# .bazelversion exists for Era 2 — bazelisk selects the right version
# Skip bazel build in prepare.sh (heavy), done at test time
bazel version || true
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

        copy_files = " ".join(file.name for file in self.files())

        # mam reference (industry-standard per-PR Dockerfile) — 1:1:
        hardening = Image._HARDENING_BLOCK.rstrip()

        return f"""FROM {name}:{tag}

# 1. Build-time args first (overridable via --build-arg)
ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

# 2. WORKDIR before any RUN/COPY that depends on it
WORKDIR /home/{self.pr.repo}

# 3. Git checkout BEFORE copying patches (clean known state)
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

# 4. COPY scripts/patches
COPY {copy_files} /home/

# 5. Install / prep
RUN bash /home/prepare.sh

# 6. Repo cleanup / hardening (kept as-is, uses ${{BASE_COMMIT}})
{hardening}

CMD ["/bin/bash"]
"""


@Instance.register("bazelbuild", "19167_to_21889")
class BazelEra2(Instance):
    """Instance for Era 2 (PRs #19167-#21889 + backport #27463).

    These commits have .bazelversion. Bazelisk auto-downloads the
    correct Bazel version. JDK 11 is the host runtime.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    _APPLY_OPTS = "--whitespace=nowarn"

    @property
    def _BAZEL_TEST_CMD(self) -> str:
        targets = extract_test_targets(self.pr.test_patch, self.pr.fix_patch)
        return (
            f"bazel --output_user_root=/tmp/bazel-output test {targets} "
            "--build_tests_only --test_output=summary --test_tag_filters=-manual "
            "--test_timeout=600 --keep_going --jobs=6 --noshow_progress 2>&1 || true"
        )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelEra2ImageDefault(self.pr, self._config)

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_BazelEra2 = [
    '19167-19175-19177',
    '20409-20472-20520-20531-20549-20550-20568-20590-20667-20729-20758-20785-20804-20847-20861-20925',
    '20594-20602-20609-20611-20612-20625-20718-20733-20734-20749-20750-20821-20828-20840-20848-20881-20901-20903-20904',
    '21472-21477-21487-21494-21502-21503-21505-21510-21512-21524-21532-21546-21547-21549-21550-21551-21552-21555-21567-21575-21577-21588-21595-21598-21599-21605-21607',
    '21644-21662-21664-21670-21671-21683-21692-21694-21700-21703-21707-21733-21735',
    '21889-21941-22176-22186',
]
for _ni in _BUNDLE_NIS_BazelEra2:
    Instance.register('bazelbuild', _ni)(BazelEra2)
