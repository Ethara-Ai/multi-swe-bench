"""Config for very early wagoodman/dive PRs (release line <= 0.4).

These PRs predate (or introduce) the project's go.mod. To make them buildable
under modern toolchains the harness writes a synthetic go.mod with pinned
historical dependency versions before running tests. The only test changes
in this era touch the ``filetree`` package, which has a self-contained
dependency set; testing is scoped to that package.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


SYNTHETIC_GO_MOD = """module github.com/wagoodman/dive

go 1.12

require (
\tgithub.com/dustin/go-humanize v1.0.0
\tgithub.com/fatih/color v1.7.0
\tgithub.com/google/uuid v1.1.0
\tgithub.com/mattn/go-colorable v0.1.4 // indirect
\tgithub.com/mattn/go-isatty v0.0.10 // indirect
\tgithub.com/phayes/permbits v0.0.0-20190612203442-39d7c581d2ee
\tgithub.com/satori/go.uuid v1.2.1-0.20181028125025-b2ce2384e17b
\tgithub.com/sirupsen/logrus v1.4.2
\tgithub.com/spf13/viper v1.3.2
\tgolang.org/x/sys v0.0.0-20191120155948-bd437916bb0e // indirect
)
"""


class DiveImageBaseGopath(Image):
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
        return "golang:1.12-buster"

    def image_tag(self) -> str:
        return "base-gopath"

    def workdir(self) -> str:
        return "base-gopath"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """PIPELINE.md §3 reference-format base (SINGLE, shared across the era).

        Leading ``# syntax`` = §2 opt-out so DockerfileEnhancer.enhance() returns
        this verbatim instead of force-pinning this shared (constant-tag) base to
        one PR's ${BASE_COMMIT} and stripping the history every other bundle
        needs.  Light hardening (drop origin) keeps FULL history; each PR layer
        applies the strict canonical hardening at its own base.sha (§4).
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class DiveImageDefaultGopath(Image):
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
        return DiveImageBaseGopath(self.pr, self.config)

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
                "synthetic.go.mod",
                SYNTHETIC_GO_MOD,
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

# Pin historical deps via a synthetic go.mod so the older code builds under
# modern Go module rules. Only the filetree package's deps are pinned; the
# rest of the tree depends on docker libs that no longer resolve cleanly.
cp /home/synthetic.go.mod /home/{pr.repo}/go.mod
rm -f go.sum

go test -v -count=1 ./filetree/ || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
cp /home/synthetic.go.mod /home/{pr.repo}/go.mod
rm -f go.sum
go test -v -count=1 ./filetree/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --reject --whitespace=nowarn /home/test.patch || true
cp /home/synthetic.go.mod /home/{pr.repo}/go.mod
rm -f go.sum
go test -v -count=1 ./filetree/

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
# PR 77's fix_patch introduces its own go.mod/go.sum; clear them first so
# the patch applies cleanly, then replace with our pinned synthetic go.mod.
rm -f go.mod go.sum
git checkout -- go.mod go.sum 2>/dev/null || true
git apply --reject --whitespace=nowarn /home/test.patch /home/fix.patch || true
cp /home/synthetic.go.mod /home/{pr.repo}/go.mod
rm -f go.sum
go test -v -count=1 ./filetree/

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        """PIPELINE.md §4 PR-image format.  FROM the shared base (full history);
        prepare.sh resets + checks out this PR's base.sha, then the canonical
        anti-reward-hacking hardening with the LITERAL base.sha.  dependency() is
        an Image, so enhance() returns this verbatim -- hardening is spelled out
        here."""
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

"""
        # §4: hardening uses the LITERAL base.sha, not a variable.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        )
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + hardening + tail


@Instance.register("wagoodman", "dive_0_to_120")
class DIVE_0_TO_120(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DiveImageDefaultGopath(self.pr, self._config)

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

        re_pass = re.compile(r"--- PASS: (\S+)")
        re_fail = re.compile(r"--- FAIL: (\S+)")
        re_skip = re.compile(r"--- SKIP: (\S+)")

        def base_name(test_name: str) -> str:
            idx = test_name.rfind("/")
            return test_name if idx == -1 else test_name[:idx]

        for line in test_log.splitlines():
            line = line.strip()
            m = re_pass.match(line)
            if m:
                name = m.group(1)
                if name in failed_tests:
                    continue
                if name in skipped_tests:
                    skipped_tests.discard(name)
                passed_tests.add(base_name(name))
                continue
            m = re_fail.match(line)
            if m:
                name = m.group(1)
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(base_name(name))
                continue
            m = re_skip.match(line)
            if m:
                name = m.group(1)
                if name in passed_tests or name in failed_tests:
                    continue
                skipped_tests.add(base_name(name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# PIPELINE.md §11b: every dash-joined bundle value must be a registered key
# in addition to the era key, else Instance.create() raises "not registered".
_BUNDLE_NIS_DIVE_0_TO_120 = [
    "39-40-43",
    "41-48-53-54",
    "77-78-87-91-100-101-102-103-104-108-114-115-120",
]
for _ni in _BUNDLE_NIS_DIVE_0_TO_120:
    Instance.register("wagoodman", _ni)(DIVE_0_TO_120)
