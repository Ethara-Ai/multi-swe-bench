import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# --- Self-contained test-target extraction -----------------------------------
# era4 deliberately does NOT import bazel_targets.py. Inlining this logic keeps
# the config in ONE file so it can be pushed on its own without editing the
# shared helper (which the rest of the team edits for era3) -> zero merge
# conflicts. Behaviour is 1:1 with bazel_targets.extract_test_targets: derive
# only the test targets the PR's test_patch touches instead of running the whole
# //src/test/... tree (which would build thousands of unrelated targets).
# Helpers are underscore-prefixed so `from ...bazel_era4 import *` exports
# nothing that clashes with the shared bazel_targets module.
def _find_build_dirs(*patches: str) -> set:
    dirs = set()
    for patch in patches:
        if not patch:
            continue
        for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch):
            path = m.group(2)
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename in ("BUILD", "BUILD.bazel"):
                pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                dirs.add(pkg_dir)
    return dirs


def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set) -> bool:
    if not parent_dir:
        return False
    if "/test/py/" in pkg_dir:
        return True
    if "/testdata/" in pkg_dir:
        return True
    if pkg_dir.endswith("/testdata"):
        return True
    if pkg_dir.endswith("/bin"):
        return True
    if parent_dir in build_dirs:
        return True
    return False


def _extract_test_targets(test_patch: str, fix_patch: str = "") -> str:
    """Return a space-separated set of bazel test targets, or //src/test/... ."""
    all_build_dirs = _find_build_dirs(test_patch or "", fix_patch or "")

    test_files = []
    for m in re.finditer(r"diff --git a/(.+?) b/(.+)", test_patch or ""):
        path = m.group(2)
        basename = path.rsplit("/", 1)[-1] if "/" in path else path
        if "/test/" not in path and "/javatests/" not in path:
            continue
        if basename in ("BUILD", "BUILD.bazel"):
            continue
        test_files.append(path)

    if not test_files:
        return "//src/test/..."

    targets = set()
    for path in test_files:
        pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        basename = path.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename

        # Bazel shell tests map 1:1 to same-named sh_test targets
        # (src/test/shell/integration/foo_test.sh -> //.../integration:foo_test),
        # so a patched .sh test file pins its exact target instead of
        # globbing the whole package (which drags in dozens of unrelated,
        # environment-flaky integration tests that poison P2P verdicts).
        if basename.endswith("_test.sh"):
            targets.add(f"//{pkg_dir}:{stem}")
            continue

        if pkg_dir in all_build_dirs:
            targets.add(f"//{pkg_dir}/...")
            continue

        parent = pkg_dir
        found_parent_pkg = False
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in all_build_dirs:
                targets.add(f"//{parent}:{stem}")
                found_parent_pkg = True
                break
        if found_parent_pkg:
            continue

        parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
        if _likely_subdir(pkg_dir, parent_dir, all_build_dirs):
            targets.add(f"//{parent_dir}:{stem}")
        else:
            targets.add(f"//{pkg_dir}/...")

    if not targets:
        return "//src/test/..."
    return " ".join(sorted(targets))


class BazelEra4ImageBase(Image):
    """Base image for Era 4 (PRs #27934-#28343, Bazel 7.2.0-9.0.1).

    These releases use JDK 21 for compilation and runtime.
    Latest PRs also use remotejdk_25 (Bazel downloads JDK 25 at build time).
    JDK 21 on the host works for all.

    .bazelversion exists for all Era 4 commits.
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
        return "base-era4"

    def workdir(self) -> str:
        return "base-era4"

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


class BazelEra4ImageDefault(Image):
    """Per-PR image for Era 4: checkout base commit, apply patches, pre-warm."""

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
        return BazelEra4ImageBase(self.pr, self._config)

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

# .bazelversion exists for Era 4 — bazelisk selects the right version
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


@Instance.register("bazelbuild", "27934_to_28343")
class BazelEra4(Instance):
    """Instance for Era 4 (PRs #27934-#28343).

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
        targets = _extract_test_targets(self.pr.test_patch, self.pr.fix_patch)
        query_cmd = (
            f"bazel --output_user_root=/tmp/bazel-output query \"tests({targets})\" "
            "2>/dev/null | sed \"s/^/MSB_REQUESTED_TARGET /\" || true ; "
        )
        return (
            query_cmd
            + f"bazel --output_user_root=/tmp/bazel-output test {targets} "
            "--build_tests_only --test_output=summary --test_tag_filters=-manual "
            "--test_timeout=1500 --keep_going --jobs=6 --noshow_progress 2>&1 || true"
        )

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BazelEra4ImageDefault(self.pr, self._config)

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

        # The optional "<N> out of <M> " group covers sharded/flaky-retried
        # summary lines, e.g. "//t:x PASSED in 4 out of 8 in 77.8s".
        re_passed = re.compile(
            r"^(//\S+)\s+PASSED\s+(?:in\s+\d+\s+out\s+of\s+\d+\s+)?in\s+[\d.]+s",
            re.MULTILINE,
        )
        re_failed = re.compile(
            r"^(//\S+)\s+FAILED\s+(?:in\s+\d+\s+out\s+of\s+\d+\s+)?in\s+[\d.]+s",
            re.MULTILINE,
        )
        # A test whose build fails never executes; in SWE-bench semantics a test
        # that cannot build at base+test_patch but builds and passes with the
        # fix_patch is a legitimate fail-to-pass candidate (e.g. tests
        # referencing APIs introduced by the fix). Count it as failed.
        re_failed_to_build = re.compile(
            r"^(//\S+)\s+FAILED TO BUILD", re.MULTILINE
        )
        re_timeout = re.compile(
            r"^(//\S+)\s+TIMEOUT\s+(?:in\s+\d+\s+out\s+of\s+\d+\s+)?in\s+[\d.]+s",
            re.MULTILINE,
        )
        re_flaky = re.compile(r"^(//\S+)\s+FLAKY", re.MULTILINE)
        re_no_status = re.compile(r"^(//\S+)\s+NO STATUS", re.MULTILINE)

        for match in re_passed.finditer(test_log):
            passed_tests.add(match.group(1))

        for match in re_failed.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_failed_to_build.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_timeout.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_flaky.finditer(test_log):
            failed_tests.add(match.group(1))

        for match in re_no_status.finditer(test_log):
            skipped_tests.add(match.group(1))

        # Requested labels (emitted by the query pass in _BAZEL_TEST_CMD).
        # Tests bazel refused to build (broken deps) can appear only in the
        # aggregate "N fail to build" sentence, so recover their names by
        # subtracting every target named with a status from the requested set.
        re_requested = re.compile(r"^MSB_REQUESTED_TARGET\s+(//\S+)", re.MULTILINE)
        requested_tests = {m.group(1) for m in re_requested.finditer(test_log)}
        named_tests = passed_tests | failed_tests | skipped_tests
        failed_tests |= requested_tests - named_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
