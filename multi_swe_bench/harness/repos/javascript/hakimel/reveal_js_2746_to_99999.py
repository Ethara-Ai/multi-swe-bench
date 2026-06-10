"""reveal.js harness — Gulp era (PRs #2746–#99999, v4.x–v5.x).

Uses node:20-bookworm with system chromium, PUPPETEER_SKIP_DOWNLOAD,
and `gulp test` via node-qunit-puppeteer.
Covers number_interval: reveal_js_2746_to_99999
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


class RevealJsGulpImageBase(Image):
    """Base image for reveal.js Gulp era (PR #2746–#99999, v4.x–v5.x).

    Provides node:20-bookworm + chromium + a fresh git clone of reveal.js so PR
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return "base-gulp"

    def workdir(self) -> str:
        return "base-gulp"

    def extra_packages(self) -> list[str]:
        return ["chromium"]

    def extra_setup(self) -> str:
        return """ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium"""

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
        if self.extra_setup():
            sections.append(self.extra_setup())
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}')
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')
        return "\n\n".join(sections) + "\n"


class RevealJsGulpImage(Image):
    """Image for reveal.js Gulp era (PR #2746–#99999, v4.x–v5.x).

    Uses node:20-bookworm with system-installed chromium and `gulp test`.
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
        return RevealJsGulpImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def extra_packages(self) -> list[str]:
        return ["chromium"]

    def extra_setup(self) -> str:
        return """ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium"""

    def files(self) -> list[File]:
        prepare_sh = """#!/bin/bash
set -e

cd /home/{pr_repo}
git reset --hard
git checkout {pr_base_sha}

npm install --legacy-peer-deps --ignore-scripts 2>&1 || npm install --ignore-scripts 2>&1 || true

if [ -f gulpfile.js ]; then
    sed -i 's|puppeteerArgs: \\[|puppeteerArgs: ["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu",|' gulpfile.js
    sed -i "s|gulp\.task('test', gulp\.series( *'eslint', *'qunit' *))|gulp.task('test', gulp.series('qunit'))|" gulpfile.js
fi || true

if [ -d node_modules/gulp-sass ]; then
    echo "module.exports = function() {{ return require('stream').PassThrough(); }}" > node_modules/gulp-sass/index.js
fi || true
if [ -d node_modules/node-sass ]; then
    mkdir -p node_modules/node-sass/lib
    echo "module.exports = {{ info: 'mock', render: function(o,cb){{cb(null)}}, types: {{}} }}" > node_modules/node-sass/lib/binding.js
fi || true
""".format(pr_repo=self.pr.repo, pr_base_sha=self.pr.base.sha)

        run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
npm test 2>&1 || true
""".format(pr_repo=self.pr.repo)

        test_run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
npm test 2>&1 || true
""".format(pr_repo=self.pr.repo)

        fix_run_sh = """#!/bin/bash
set -e
cd /home/{pr_repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}
npm install --legacy-peer-deps --ignore-scripts 2>&1 || true
npm test 2>&1 || true
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
            f"{self.extra_setup()}\n"
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


@Instance.register("hakimel", "reveal_js_2746_to_99999")
class RevealJsGulpInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealJsGulpImage(self.pr, self._config)

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
        """Parse node-qunit-puppeteer output (gulp qunit task).

        Extracts identifiable per-test names so F2P/P2P transitions can be
        detected across run/test/fix stages.

        Lines of interest:
          ✔ test/test-core.html [293/293] in 1234ms     -> file pass
          ✘ test/test-core.html [292/293] in 1234ms     -> file partial
          Test: <name>                                   -> followed by Status:
              Status: passed | failed | skipped
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Two-line pattern for per-test results
        file_pass_re = re.compile(
            r"[✔✓]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]"
        )
        file_fail_re = re.compile(
            r"[!✘✗×✕✖]\s+(\S+\.html)\s+\[(\d+)/(\d+)\]"
        )
        test_name_re = re.compile(r"^\s*Test:\s+(.+?)\s*$")
        status_re = re.compile(r"^\s*Status:\s+(passed|failed|skipped)\s*$")

        lines = [ansi_escape.sub("", ln) for ln in test_log.splitlines()]

        pending_test: Optional[str] = None
        for raw in lines:
            line = raw.rstrip()
            if not line.strip():
                continue

            # Per-test "Test: <name>" then "Status: <s>"
            m = test_name_re.match(line)
            if m:
                pending_test = m.group(1).strip()
                continue
            m = status_re.match(line)
            if m and pending_test is not None:
                status = m.group(1)
                if status == "passed":
                    passed_tests.add(pending_test)
                elif status == "failed":
                    failed_tests.add(pending_test)
                else:
                    skipped_tests.add(pending_test)
                pending_test = None
                continue

            stripped = line.strip()

            # ✔ test/foo.html [N/N]  -> all subtests of file passed
            m = file_pass_re.match(stripped)
            if m and m.group(2) == m.group(3):
                passed_tests.add("file:" + m.group(1))
                continue

            # ✘/! test/foo.html [M/N]  -> file has at least one failure
            m = file_fail_re.match(stripped)
            if m:
                failed_tests.add("file:" + m.group(1))
                continue

        # If a file appears in BOTH (per-test failure + file-level pass), trust the failure.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
