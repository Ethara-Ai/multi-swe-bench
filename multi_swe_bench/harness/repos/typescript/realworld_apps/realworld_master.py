import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Era 1 -- the original "Conduit spec" repo (base ref `master`).
# Tests are Postman/Newman collections run against a live remote API
# (https://conduit.productionready.io). There is no per-test unit output
# the harness can parse, so this config builds and runs cleanly but is
# expected to yield 0 parsed tests (instances remain unresolved).


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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base-master"

    def workdir(self) -> str:
        return "base-master"

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

WORKDIR /home/
RUN apt update && apt install -y git jq

{code}

{self.clear_env}

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

""".format(),
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
npm install --no-audit --progress=false || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
npm test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm test || true

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


@Instance.register("realworld-apps", "realworld_master")
class RealworldMaster(Instance):
    # Skip lockfiles and binary assets (icons/fonts/etc.): `git apply` aborts
    # the whole patch on a binary hunk that lacks a full index line.
    _APPLY_EXCLUDES = (
        "--exclude=package-lock.json --exclude=pnpm-lock.yaml "
        "--exclude=yarn.lock --exclude=*.ico --exclude=*.png "
        "--exclude=*.jpg --exclude=*.jpeg --exclude=*.gif --exclude=*.webp "
        "--exclude=*.svg --exclude=*.woff --exclude=*.woff2 --exclude=*.ttf "
        "--exclude=*.otf --exclude=*.eot --exclude=*.mp4 --exclude=*.pdf "
        "--exclude=*.ttc"
    )

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def _apply(self, patches: str) -> str:
        g = f"git apply {self._APPLY_EXCLUDES} --whitespace=nowarn"
        return f"{{ {g} {patches} || {g} --reject {patches} || true; }}"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f"bash -c 'cd /home/{self.pr.repo}; set -f; npm test || true'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo}; set -f; "
            f"{self._apply('/home/test.patch')}; "
            f"npm test || true'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo}; set -f; "
            f"{self._apply('/home/test.patch /home/fix.patch')}; "
            f"npm test || true'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # Newman per-request assertion lines, if a live API run produced any:
        #   "  ✓  Status code is 200"
        #   "  ✗  Body is correct"
        re_pass = re.compile(r"^✓\s+(.+?)$")
        re_fail = re.compile(r"^[✗✕×]\s+(.+?)$")

        for raw in test_log.splitlines():
            clean = ansi_re.sub("", raw).strip()
            if not clean:
                continue

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1))
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(fail_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
