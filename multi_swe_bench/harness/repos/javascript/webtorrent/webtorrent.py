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
        return "node:20-bookworm"

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

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y git build-essential python3

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

""".format(pr=self.pr),
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

# PRs <= 2142 have a broken git dependency (http-node) that references
# a non-existent branch. This dep is browser-only and not needed for
# Node.js tests. Remove it before installing to unblock npm install.
if node -e "
  const pkg = JSON.parse(require('fs').readFileSync('package.json', 'utf8'));
  const dep = pkg.dependencies && pkg.dependencies['http-node'];
  if (dep && dep.startsWith('github:')) {{
    delete pkg.dependencies['http-node'];
    require('fs').writeFileSync('package.json', JSON.stringify(pkg, null, 2));
    console.log('Removed broken http-node git dependency');
  }}
"; then
  echo "Dependency check complete"
fi

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
timeout 600 ./node_modules/.bin/tape test/node/*.js test/*.js || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install || true
timeout 600 ./node_modules/.bin/tape test/node/*.js test/*.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install || true
timeout 600 ./node_modules/.bin/tape test/node/*.js test/*.js || true

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


@Instance.register("webtorrent", "webtorrent")
class Webtorrent(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        test_log = ansi_escape.sub("", test_log)

        # tape TAP format: "ok N description" (no dash separator, unlike node-tap's "ok N - description")
        re_tap_version = re.compile(r"^TAP version \d+")
        re_tap_test = re.compile(
            r"^\s*(ok|not ok)\s+\d+\s+(.+?)(?:\s+#\s*(SKIP|TODO)\b.*)?\s*$",
            re.IGNORECASE,
        )
        re_tap_group = re.compile(r"^# (.+)$")
        re_tap_plan = re.compile(r"^1\.\.\d+$")
        re_tap_summary = re.compile(r"^# (tests|pass|fail|ok|not ok)\b")

        current_group = ""

        lines = test_log.splitlines()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if re_tap_version.match(stripped):
                continue
            if re_tap_plan.match(stripped):
                continue
            if re_tap_summary.match(stripped):
                continue

            group_match = re_tap_group.match(stripped)
            if group_match:
                current_group = group_match.group(1).strip()
                continue

            test_match = re_tap_test.match(stripped)
            if test_match:
                status = test_match.group(1)
                test_name = test_match.group(2).strip()
                directive = test_match.group(3)

                if current_group:
                    full_name = f"{current_group}: {test_name}"
                else:
                    full_name = test_name

                if directive and directive.upper() in ("SKIP", "TODO"):
                    skipped_tests.add(full_name)
                elif status == "ok":
                    passed_tests.add(full_name)
                elif status == "not ok":
                    failed_tests.add(full_name)
                continue

        for test in failed_tests:
            passed_tests.discard(test)
            skipped_tests.discard(test)
        for test in skipped_tests:
            passed_tests.discard(test)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
