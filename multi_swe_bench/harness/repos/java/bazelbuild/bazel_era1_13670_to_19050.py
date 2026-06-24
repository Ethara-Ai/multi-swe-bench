import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.bazelbuild.bazel_targets import (
    extract_test_targets,
)


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
        return "base-era1"

    def workdir(self) -> str:
        return "base-era1"

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
        # Scope to only the test targets the PR touches (fast); fall back to the
        # full tree only when nothing can be derived.
        targets = extract_test_targets(self.pr.test_patch, self.pr.fix_patch)
        return (
            f"export USE_BAZEL_VERSION={self._bazel_version} ; "
            f"bazel --output_user_root=/tmp/bazel-output test {targets} "
            "--build_tests_only --test_output=summary --test_tag_filters=-manual "
            "--test_timeout=600 --keep_going --jobs=6 --noshow_progress 2>&1 || true"
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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_BazelEra1 = [
    '16450-16462-16478',
    '16668-16669-16672-16702-16745-16819-16880-16884-16891-16892-16964',
    '19050-19052-19071-19086-19089-19091-19106',
    '13670-13671-13675-13677-13683-13686-13688-13697-13705-13714-13729-13733-13735-13738-13763-13768-13775-13777-13838',
    '13893-13906',
    '14318-14319-14324-14325',
    '14737-14748-14749-14750-14751-14752-14753-14754-14755-14757-14769-14773-14779-14780-14794-14795-14813-14832-14833-14834-14835-14836-14842-14860-14885-14899-14923-14925-14929-14930-14943-14945-14952-14954-14956-14958-14984-14986-14989-14990-14991-14995-14997-14998-14999-15020-15046-15047-15048-15065-15066-15068-15071-15085-15087-15090-15091-15102',
    '15181-15183-15190-15200',
    '15216-15217-15218-15223-15231-15266-15298-15299-15341-15342-15372-15383-15395-15404-15405-15429-15431-15433-15434-15453-15471-15472-15473-15474-15534-15559-15585-15600',
    '15703-15708-15709-15718-15719-15737-15754-15766-15768-15769-15772-15782-15784-15787-15788-15793-15815-15816-15818-15819-15824-15842-15844-15852-15854-15870-15883-15900-15907-15910-15911-15922-15923-15929-15931-15940-15942-15943-15965-15970-15971-15973-15976-15979-15984-15986-15998-16025-16030-16032-16036-16045-16079-16110-16113',
    '17216-17217-17229-17234-17247-17253-17263-17269-17272-17273-17274-17275-17278-17284-17285-17287-17288-17290-17299-17306-17307-17309-17311-17312-17313-17324-17325-17326-17327-17329-17331-17343-17344-17352-17360-17365-17371-17396-17397-17398-17422-17435-17438-17439-17440-17445-17459-17465-17488-17491-17494-17496-17504-17505-17506-17512-17513-17521-17528-17538-17539-17544-17557-17563-17570-17578-17583-17589-17592-17594-17601-17605-17613-17616-17617-17619-17632-17641',
    '17696-17757',
    '17977-17986-17996-18115-18123',
    '18039-18045-18082-18101',
    '18503-18504-18512-18533-18535',
]
for _ni in _BUNDLE_NIS_BazelEra1:
    Instance.register('bazelbuild', _ni)(BazelEra1)
