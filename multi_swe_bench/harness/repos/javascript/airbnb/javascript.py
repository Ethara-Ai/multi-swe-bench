import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR of this repo.

    dependency() returns a *string*, so this Dockerfile carries no ``# syntax``
    directive and the DockerfileEnhancer engages, prepending the
    ``# syntax``/ARG/ENV/LABEL infra block.

    IMPORTANT: this image must NOT clone the repository. The enhancer rewrites
    any clone/COPY of the repo into a ``${BASE_COMMIT}``-pinned clone followed
    by the history-stripping hardening block. On an image shared per-repo (tag
    "base", built once) that would force-pin the shared base to one PR's commit
    and delete every other ref, so `git checkout <base.sha>` would fail for
    every other PR sharing it. The clone therefore lives in ImageDefault, which
    depends on an Image and is left verbatim by the enhancer, and is done
    per-PR. This image only provides the Node toolchain.
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

    def dependency(self) -> str:
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        default_packages = [
            "ca-certificates",
            "curl",
            "build-essential",
            "git",
            "gnupg",
            "make",
            "python3",
            "sudo",
            "wget",
        ]
        all_packages = default_packages + self.extra_packages()
        packages_str = " \\\n    ".join(all_packages)
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""FROM {base_img}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

{apt_command}

{self.clear_env}

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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
# The repo is cloned and checked out at the base commit inline in the Dockerfile
# and hardened there, so this only resets the working tree and installs deps.
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}

for pkg in packages/eslint-config-airbnb-base packages/eslint-config-airbnb; do
    if [ -d "$pkg" ] && [ -f "$pkg/package.json" ]; then
        echo "Running tests in $pkg..."
        cd "$pkg"
        npm run tests-only
        cd /home/{pr.repo}
    fi
done
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch

for pkg in packages/eslint-config-airbnb-base packages/eslint-config-airbnb; do
    if [ -d "$pkg" ] && [ -f "$pkg/package.json" ]; then
        echo "Running tests in $pkg..."
        cd "$pkg"
        npm run tests-only
        cd /home/{pr.repo}
    fi
done
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch

for pkg in packages/eslint-config-airbnb-base packages/eslint-config-airbnb; do
    if [ -d "$pkg" ] && [ -f "$pkg/package.json" ]; then
        echo "Running tests in $pkg..."
        cd "$pkg"
        npm run tests-only
        cd /home/{pr.repo}
    fi
done
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

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history and checks out ${BASE_COMMIT} inline. Because dependency()
        # is an Image, the DockerfileEnhancer returns this Dockerfile verbatim --
        # the clone and hardening below are kept as written (and pinning here is
        # correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("airbnb", "javascript")
class AirbnbJavaScript(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape sequences
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        for line in clean_log.splitlines():
            line = line.strip()

            ok_match = re.match(r"^ok\s+\d+\s*(?:[-–]\s*)?(.+)$", line)
            if ok_match:
                test_name = ok_match.group(1).strip()
                if test_name:
                    passed_tests.add(test_name)
                continue

            not_ok_match = re.match(r"^not\s+ok\s+\d+\s*(?:[-–]\s*)?(.+)$", line)
            if not_ok_match:
                test_name = not_ok_match.group(1).strip()
                if test_name:
                    failed_tests.add(test_name)
                continue

            bare_ok = re.match(r"^ok\s+\d+$", line)
            if bare_ok:
                passed_tests.add(f"test_{bare_ok.group(0)}")
                continue

            bare_not_ok = re.match(r"^not\s+ok\s+\d+$", line)
            if bare_not_ok:
                failed_tests.add(f"test_{bare_not_ok.group(0)}")
                continue

        # Ensure no test is counted as both passing and failing
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
