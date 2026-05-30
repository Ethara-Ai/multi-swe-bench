from __future__ import annotations

import os
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    ".pdf", ".zip", ".jar", ".class", ".tar", ".gz", ".tgz", ".bz2", ".7z",
    ".bin", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp3", ".mp4", ".mov", ".wav", ".ogg",
    ".db", ".sqlite", ".rdb",
    ".odex", ".oat", ".vdex", ".art",
    ".nupkg", ".whl", ".egg", ".gem",
    ".rpm", ".deb", ".apk",
}


def _is_binary_path(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    if ext in _BINARY_EXTENSIONS:
        return True
    return False


def _strip_binary_hunks(patch_text: str) -> str:
    """Remove binary-file hunks from a unified diff so `git apply` won't reject
    the whole patch when only binary fixture hunks are problematic.
    Binary content is irrelevant to Go test outcomes in this repo."""
    if not patch_text:
        return patch_text
    out = []
    blocks = re.split(r"(?m)^(?=diff --git )", patch_text)
    for block in blocks:
        if not block.strip():
            continue
        first_line = block.splitlines()[0]
        m = re.match(r"diff --git a/(.*) b/(.*)$", first_line)
        if m and (_is_binary_path(m.group(1)) or _is_binary_path(m.group(2))):
            continue
        if "GIT binary patch" in block or re.search(
            r"^Binary files .* differ", block, re.MULTILINE
        ):
            continue
        out.append(block)
    return "".join(out)


class SyftImageBase(Image):
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
        return "golang:1.25-bookworm"

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
ENV GOTOOLCHAIN=auto

# System deps used across syft eras (Go 1.16 -> 1.25):
#   - git/ca-certificates: required for `go mod` over HTTPS and patch application
#   - build-essential/pkg-config: cgo compilation for some indirect deps
#   - zip/unzip: archive cataloger tests build zip fixtures via shell `zip`
#   - file: a few cataloger tests shell out to `file` for magic detection
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config \\
    zip unzip file \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class SyftImageDefault(Image):
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
        return SyftImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                _strip_binary_hunks(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_hunks(self.pr.test_patch),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export GOTOOLCHAIN=auto
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true

# NOTE: previously ran `go test -count=1 ./...` here as a cache-warm step, but
# that step ran twice under amd64 QEMU during multi-arch build and dominated
# wall time (~45 min/PR). Test stages later run natively on arm64 anyway, so
# warming under QEMU buys nothing. Removed → ~4x faster build phase.

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=auto
export CI=true
cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=auto
export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export GOTOOLCHAIN=auto
export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

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


@Instance.register("anchore", "syft")
class Syft(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SyftImageDefault(self.pr, self._config)

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

        # Strip ANSI escape sequences first — Go test output is usually plain,
        # but `-v` runs through some wrappers/CI runners can inject color codes
        # that would otherwise break the simple `--- PASS: <name>` regex.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        # Final invariant enforcement: TestResult requires disjoint sets.
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
