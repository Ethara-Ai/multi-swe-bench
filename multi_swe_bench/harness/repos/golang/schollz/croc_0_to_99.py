import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "golang:1.13"

    def image_tag(self) -> str:
        return "base-gopath"

    def workdir(self) -> str:
        return "base-gopath"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        gopath_pkg = f"github.com/{org}/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    GOPATH=/go \\
    GO111MODULE=off \\
    PATH=/go/bin:$PATH

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

# Debian Buster is EOL -- switch to archive.debian.org
RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list && \\
    sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

{self.global_env}

RUN mkdir -p /go/src/{gopath_pkg}
RUN git clone "${{REPO_URL}}" /go/src/{gopath_pkg}

WORKDIR /go/src/{gopath_pkg}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go test -p 1 -v ./... > /dev/null 2>&1 || true

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
go test -p 1 -v ./... 2>&1 | tr -cd '\11\12\15\40-\176'

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -p 1 -v ./... 2>&1 | tr -cd '\11\12\15\40-\176'

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /go/src/{gopath_pkg}
git apply /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject /home/test.patch 2>&1 || true; git apply --reject /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
go test -p 1 -v ./... 2>&1 | tr -cd '\11\12\15\40-\176'

""".format(pr=self.pr, gopath_pkg=gopath_pkg),
            ),
        ]

    def dockerfile(self) -> str:
        """PIPELINE.md §4 PR-image format, GOPATH variant.

        IMPORTANT: this era predates Go modules, so the checkout lives at
        /go/src/github.com/{org}/{repo}, NOT /home/{repo}.  The hardening block
        must therefore run with WORKDIR set to the GOPATH package dir -- the
        canonical block is CWD-relative.  (This is also why the base carries the
        ``# syntax`` opt-out: the enhancer's auto-injection assumes /home/{repo}
        and would run the hardening against a non-existent directory here.)

        dependency() is an Image, so enhance() returns this Dockerfile verbatim
        and the hardening must be spelled out explicitly.
        """
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        gopath_pkg = f"github.com/{self.pr.org}/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /go/src/{gopath_pkg}

"""

        # Canonical Image._HARDENING_BLOCK, concatenated raw so its
        # ${BASE_COMMIT} / %(refname) tokens stay literal for the shell.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("schollz", "croc_0_to_99")
class Croc_0_to_99(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

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

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md §11b: the JSONL and this registry ship together, and the
# trajectory harness routes via Instance.create() -> f"{org}/{number_interval}".
# Every dash-joined bundle value must therefore be a registered key, in
# addition to the era key above, else create() raises "not registered".
_BUNDLE_NIS_Croc_0_to_99 = [
    "41-42-43-45-49-51",
    "52-55",
    "63-64",
]
for _ni in _BUNDLE_NIS_Croc_0_to_99:
    Instance.register("schollz", _ni)(Croc_0_to_99)
