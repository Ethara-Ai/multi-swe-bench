import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.bazelbuild.bazel_targets import (
    extract_test_targets,
)


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
        return "base-era3"

    def workdir(self) -> str:
        return "base-era3"

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_BazelEra3 = [
    '25310-25331-25344-25348',
    '25788-25802',
    '27411-27418-27436-27437',
    '22381-22383-22384-22385-22391-22407-22413-22416-22422-22450-22451-22460-22467-22473-22478-22479-22480-22500-22506-22507-22508-22518-22524-22525-22537-22539-22540-22541-22557-22562-22563-22570-22572-22573-22575-22583-22628-22634-22635-22636-22637-22647-22648-22650',
    '22697-22699-22707-22713-22758-22784-22785-22790-22806-22814-22823-22828-22831-22837',
    '23281-23290-23304-23309',
    '23974-23979-23998-24000-24001-24017-24030',
    '24084-24086-24087-24111-24114-24122-24123-24132-24153-24211-24228',
    '24628-24630-24644-24648-24650-24660-24726-24727-24741-24753-24769-24771-24772-24793-24815-24821-24845-24852-24856-24874-24877-24895-24901-24909-24918-24924',
    '24665-24667-24682-24691-24722-24725-24732-24812-24813-24835-24844-24846-24847-24866-24878-24881-24900-24908-24912-24915-24939-24941-25000-25025-25057-25093-25115',
    '24853-24855-24872-24873-24913-24914-24966-24967-24968-24969-24970-24971-24972-24974-24975-24976-24977-24978-24988-24989-24991-24992-24993-24994-24999-25004-25007-25008-25009-25014-25016-25017-25018-25019-25020-25021-25022-25023-25026-25027-25028-25031-25032-25037-25038-25051-25053-25054-25058-25064-25065-25072-25080-25085-25086-25094-25095-25097-25098-25102-25103-25104-25105-25110-25111-25117-25126-25127-25133-25137-25138-25139-25140-25146-25152-25153-25157-25161-25162-25174-25178-25180-25182-25184-25186-25191-25202-25211-25213-25225-25230-25240-25242-25251',
    '25424-25467-25473-25474-25489-25490-25491-25492-25509-25512-25548-25549-25560-25598-25605-25613-25614-25624-25632-25658',
    '25848-25850-25856-25857',
    '26296-26317-26319-26323',
    '26372-26373-26374-26375-26412',
    '27160-27167',
    '27463-28193',
    '27548-27561-27601',
    '27679-27705-27753-27774-27785-27789-27864-27882-27904-27908',
    '27994-27995-27996-28027-28040-28116-28161-28181',
    '28360-28397-28465-28502-28516-28548-28550-28552-28586-28622-28640-28649-28677-28679-28687-28714-28756-28767-28775-28785-28817-28835-28854-28864-28889-28895-28908',
    '28951-28990-29010-29012-29194-29214-29235-29238-29251',
]
for _ni in _BUNDLE_NIS_BazelEra3:
    Instance.register('bazelbuild', _ni)(BazelEra3)
