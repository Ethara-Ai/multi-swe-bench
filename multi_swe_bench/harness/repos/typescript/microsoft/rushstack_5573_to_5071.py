from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "rushstack"


class rushstack_5573_to_5071_ImageBase(Image):
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
        return "node:22"

    def image_tag(self) -> str:
        return "base-node22"

    def workdir(self) -> str:
        return "base-node22"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {REPO_DIR} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class rushstack_5573_to_5071_ImageDefault(Image):
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
        return rushstack_5573_to_5071_ImageBase(self.pr, self._config)

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
                "detect_projects.sh",
                """#!/bin/bash
PATCH_FILE="$1"
if [ ! -f "$PATCH_FILE" ]; then
    exit 0
fi
grep '^diff --git' "$PATCH_FILE" | sed 's|diff --git a/||;s| b/.*||' | while read -r fpath; do
    dir="$fpath"
    while [ "$dir" != "." ] && [ -n "$dir" ]; do
        if [ -f "/home/{repo_dir}/$dir/package.json" ]; then
            pkg_name=$(node -e "try{{console.log(require('/home/{repo_dir}/'+'$dir'+'/package.json').name)}}catch(e){{}}")
            if [ -n "$pkg_name" ]; then
                echo "$pkg_name"
            fi
            break
        fi
        dir=$(dirname "$dir")
    done
done | sort -u
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

sed -i 's/"strictPeerDependencies": true/"strictPeerDependencies": false/' rush.json
node common/scripts/install-run-rush.js install --bypass-policy || true

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js test 2>&1
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js test --to "$proj" 2>&1
    done
fi
""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
node common/scripts/install-run-rush.js install --bypass-policy || true

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js test 2>&1
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js test --to "$proj" 2>&1
    done
fi

""".format(pr=self.pr, repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo_dir}
if ! git -C /home/{repo_dir} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
node common/scripts/install-run-rush.js install --bypass-policy || true

PROJECTS=$(bash /home/detect_projects.sh /home/test.patch)
if [ -z "$PROJECTS" ]; then
    node common/scripts/install-run-rush.js test 2>&1
else
    for proj in $PROJECTS; do
        node common/scripts/install-run-rush.js test --to "$proj" 2>&1
    done
fi

""".format(pr=self.pr, repo_dir=REPO_DIR),
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


@Instance.register("microsoft", "rushstack_5573_to_5071")
class rushstack_5573_to_5071(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return rushstack_5573_to_5071_ImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

        # Rush project-level test output format:
        # "@package/name (test)" completed successfully in X.XX seconds.
        # "@package/name (test)" failed to build.
        # "@package/name (test)" did not define any work.
        # "@package/name (test)" is blocked by "...".
        re_test_pass = re.compile(
            r'^"(.+?)\s+\(test\)"\s+completed successfully in .+$'
        )
        re_test_fail = re.compile(
            r'^"(.+?)\s+\(test\)"\s+failed to build\.$'
        )
        re_test_blocked = re.compile(
            r'^"(.+?)\s+\(test\)"\s+is blocked by .+$'
        )
        re_test_noop = re.compile(
            r'^"(.+?)\s+\(test\)"\s+did not define any work\.$'
        )

        # Rush project-level BUILD output format:
        re_build_pass = re.compile(
            r'^"(.+?)\s+\(build\)"\s+completed successfully in .+$'
        )
        re_build_fail = re.compile(
            r'^"(.+?)\s+\(build\)"\s+failed to build\.$'
        )
        re_build_warning = re.compile(
            r'^"(.+?)\s+\(build\)"\s+completed with warnings in .+$'
        )
        re_build_blocked = re.compile(
            r'^"(.+?)\s+\(build\)"\s+is blocked by .+$'
        )
        re_build_noop = re.compile(
            r'^"(.+?)\s+\(build\)"\s+did not define any work\.$'
        )

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_test_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_test_blocked.match(line)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re_test_pass.match(line)
            if m:
                test_name = m.group(1).strip()
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                continue

            m = re_test_noop.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

            m = re_build_fail.match(line)
            if m:
                failed_tests.add(m.group(1).strip() + " (build)")
                continue

            m = re_build_blocked.match(line)
            if m:
                failed_tests.add(m.group(1).strip() + " (build)")
                continue

            m = re_build_warning.match(line)
            if m:
                failed_tests.add(m.group(1).strip() + " (build)")
                continue

            m = re_build_pass.match(line)
            if m:
                build_name = m.group(1).strip() + " (build)"
                if build_name not in failed_tests:
                    passed_tests.add(build_name)
                continue

            m = re_build_noop.match(line)
            if m:
                skipped_tests.add(m.group(1).strip() + " (build)")
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
