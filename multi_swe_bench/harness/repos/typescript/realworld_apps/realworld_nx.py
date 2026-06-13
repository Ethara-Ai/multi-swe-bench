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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base-nx"

    def workdir(self) -> str:
        return "base-nx"

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
npm ci --legacy-peer-deps --no-audit --progress=false || npm install --legacy-peer-deps --no-audit --progress=false || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
node_modules/.bin/nx test api --skip-nx-cache --verbose || npx --no-install nx test api --skip-nx-cache --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
node_modules/.bin/nx test api --skip-nx-cache --verbose || npx --no-install nx test api --skip-nx-cache --verbose || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
node_modules/.bin/nx test api --skip-nx-cache --verbose || npx --no-install nx test api --skip-nx-cache --verbose || true

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


@Instance.register("realworld-apps", "realworld_nx")
class RealworldNx(Instance):
    # Install deps. Three guarded steps:
    #  1. install if no runner binary is present;
    #  2. if the @nx/jest preset package is missing, force a full reinstall
    #     (an aborted `npm ci` -- e.g. the amd64/QEMU @swc/core postinstall
    #     segfault -- leaves node_modules incomplete); --ignore-scripts keeps
    #     the install from aborting on native postinstall steps;
    #  3. fall back to pnpm for the later turbo/pnpm-workspace commits.
    _NPM = "--legacy-peer-deps --no-audit --progress=false --ignore-scripts"
    _ENSURE = (
        "{ [ -x node_modules/.bin/nx ] || [ -x node_modules/.bin/jest ] || "
        f"npm ci {_NPM} || npm install {_NPM} || true; }}; "
        "{ [ -d node_modules/@nx/jest ] || [ -d node_modules/@nrwl/jest ] || "
        f"npm ci {_NPM} || npm install {_NPM} || true; }}; "
        "{ [ -x node_modules/.bin/nx ] || [ -x node_modules/.bin/jest ] || "
        "(corepack enable >/dev/null 2>&1; npm i -g pnpm@8 >/dev/null 2>&1; "
        "pnpm install --no-frozen-lockfile) || true; }"
    )
    # The api project is the only one with runnable Jest unit tests (demo /
    # cypress / playwright need a live server + browser). Prefer `nx test`,
    # but fall back to invoking Jest directly when nx itself is broken at a
    # given commit (missing nx-cloud module, stale runner config, no nx).
    _TEST_CMD = (
        "node_modules/.bin/nx test api --skip-nx-cache --verbose || "
        "node_modules/.bin/jest --config apps/api/jest.config.ts --verbose --ci || "
        "node_modules/.bin/jest --projects apps/api --verbose --ci || true"
    )
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
        # Tolerant apply: drop binary/lockfile paths, fall back to --reject
        # so the text hunks still land even if something does not apply.
        g = f"git apply {self._APPLY_EXCLUDES} --whitespace=nowarn"
        return f"{{ {g} {patches} || {g} --reject {patches} || true; }}"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo}; set -f; "
            f"{self._ENSURE}; {self._TEST_CMD}'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo}; set -f; "
            f"{self._apply('/home/test.patch')}; "
            f"{self._ENSURE}; {self._TEST_CMD}'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo}; set -f; "
            f"{self._apply('/home/test.patch /home/fix.patch')}; "
            f"{self._ENSURE}; {self._TEST_CMD}'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # File-level result line: "PASS api apps/api/src/tests/foo.test.ts"
        re_file = re.compile(r"^(?:PASS|FAIL)\s+\S+\s+(\S+\.(?:test|spec)\.[tj]sx?)")
        # Per-test lines (Jest verbose). Optional trailing "(N ms)".
        re_pass = re.compile(r"^✓\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_fail = re.compile(r"^[✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_skip = re.compile(
            r"^[○✎]\s+(?:skipped\s+|todo\s+)?(.+?)(?:\s+\(\d+\s*m?s\))?$"
        )

        current_file = ""
        for raw in test_log.splitlines():
            clean = ansi_re.sub("", raw).strip()
            if not clean:
                continue

            file_match = re_file.match(clean)
            if file_match:
                current_file = file_match.group(1)
                continue

            prefix = f"{current_file}::" if current_file else ""

            pass_match = re_pass.match(clean)
            if pass_match:
                passed_tests.add(prefix + pass_match.group(1))
                continue

            fail_match = re_fail.match(clean)
            if fail_match:
                failed_tests.add(prefix + fail_match.group(1))
                continue

            skip_match = re_skip.match(clean)
            if skip_match:
                skipped_tests.add(prefix + skip_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
