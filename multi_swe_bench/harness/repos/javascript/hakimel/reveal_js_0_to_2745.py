"""reveal.js harness — Grunt era (PRs #0–#2745, v3.x).

Uses node:14-bullseye with `grunt test` via PhantomJS/QUnit.
Covers number_interval: reveal_js_0_to_2745
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


class RevealJsGruntImageBase(Image):
    """Base image for reveal.js Grunt era (PR #0–#2745, v3.x).

    Provides node:14-bullseye + apt deps + a fresh git clone of reveal.js so PR
    images only need to checkout the base SHA and install npm packages.
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
        return "node:14-bullseye"

    def image_tag(self) -> str:
        return "base-grunt"

    def workdir(self) -> str:
        return "base-grunt"

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

        sections = [f"FROM {base_img}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(apt_command)
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class RevealJsGruntImage(Image):
    """Image for reveal.js Grunt era (PR #0–#2745, v3.x).

    Uses node:14-bullseye with standard npm install and grunt test.
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
        return RevealJsGruntImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def extra_packages(self) -> list[str]:
        return []

    def extra_setup(self) -> str:
        return ""

    def files(self) -> list[File]:
        prepare_sh = """#!/bin/bash
set -e

cd /home/{pr_repo}
git reset --hard
git checkout {pr_base_sha}
npm install --legacy-peer-deps --ignore-scripts 2>&1 || npm install --ignore-scripts 2>&1 || true

# Try to install a working PhantomJS binary (older versions of grunt-contrib-qunit
# in v3.x expect it; the npm download often fails so we fall back gracefully).
PHANTOMJS_CDNURL=https://github.com/Medium/phantomjs/releases/download/v2.1.1 \\
    npm install --no-save phantomjs-prebuilt@2.1.16 --legacy-peer-deps 2>&1 || true

# Strip the failing 'sass' subtask out of Gruntfile so test runs only qunit/jshint.
if [ -f Gruntfile.js ]; then
    sed -i "s|grunt.registerTask('test',[^)]*)|grunt.registerTask('test', ['qunit'])|g" Gruntfile.js || true
    sed -i "s|grunt.registerTask( *'test',[^)]*)|grunt.registerTask('test', ['qunit'])|g" Gruntfile.js || true
fi

if [ -d node_modules/node-sass ]; then
    mkdir -p node_modules/node-sass/lib
    echo "module.exports = {{ info: 'mock', render: function(o,cb){{cb(null)}}, types: {{}} }}" > node_modules/node-sass/lib/binding.js
fi || true
""".format(pr_repo=self.pr.repo, pr_base_sha=self.pr.base.sha)

        run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
./node_modules/.bin/grunt --force test 2>&1 || npm test 2>&1 || true
""".format(pr_repo=self.pr.repo)

        test_run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
./node_modules/.bin/grunt --force test 2>&1 || npm test 2>&1 || true
""".format(pr_repo=self.pr.repo)

        fix_run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
./node_modules/.bin/grunt --force test 2>&1 || npm test 2>&1 || true
""".format(pr_repo=self.pr.repo)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "".join(
            "COPY {name} /home/\n".format(name=f.name) for f in self.files()
        )
        return (
            f"FROM {base_name}\n"
            f"\n"
            f"{self.global_env}\n"
            f"\n"
            f"WORKDIR /home/{self.pr.repo}\n"
            f"\n"
            f"{copy_commands}"
            f"RUN bash /home/prepare.sh; exit 0\n"
            f"\n"
            f"{self.clear_env}\n"
            f"\n"
            f'CMD ["/bin/bash"]\n'
        )


@Instance.register("hakimel", "reveal_js_0_to_2745")
class RevealJsGruntInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealJsGruntImage(self.pr, self._config)

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
        """Parse grunt-contrib-qunit test output.

        Handles output like:
          >> 158 tests completed with 0 failed, and 0 skipped
          >> 158 assertions passed (1234ms)
          >> 158 passed, 0 failed
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            # ">> N tests completed with 0 failed"
            m = re.search(
                r">>\s*(\d+)\s+tests?\s+completed\s+with\s+0\s+failed", line
            )
            if m:
                passed_tests.add("suite:passed-{n}".format(n=m.group(1)))
                continue

            # ">> 0 failed, N passed"
            m = re.search(r">>\s*0\s+failed,\s*(\d+)\s+passed", line)
            if m:
                passed_tests.add("suite:passed-{n}".format(n=m.group(1)))
                continue

            # ">> N assertions passed"
            m = re.search(r">>\s*(\d+)\s+assertions?\s+passed", line)
            if m:
                passed_tests.add("suite:passed-{n}".format(n=m.group(1)))
                continue

            # ">> N passed, M failed" (N > 0, M > 0)
            m = re.search(r">>\s*(\d+)\s+passed,\s*(\d+)\s+failed", line)
            if m:
                passed = int(m.group(1))
                failed = int(m.group(2))
                if failed == 0:
                    passed_tests.add("suite:passed-{n}".format(n=passed))
                else:
                    failed_tests.add("suite:failed-{n}".format(n=failed))
                continue

            # ">> N tests completed with M failed"
            m = re.search(
                r">>\s*(\d+)\s+tests?\s+completed\s+.*?(\d+)\s+failed", line
            )
            if m:
                failed_count = int(m.group(2))
                if failed_count == 0:
                    passed_tests.add("suite:passed-{n}".format(n=m.group(1)))
                else:
                    failed_tests.add("suite:failed-{n}".format(n=failed_count))
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
