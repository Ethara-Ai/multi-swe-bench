import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPORTER = """const path = require('path');

function MochaIds(runner) {
    const root = process.cwd();
    const clean = (s) => String(s).replace(/\\s+/g, ' ').trim();
    const id = (t) => {
        const file = t.file ? path.relative(root, t.file) : 'unknown';
        return [file].concat(t.titlePath().map(clean)).join(' > ');
    };
    runner.on('pass', (t) => console.log('MOCHA_RESULT PASS ' + id(t)));
    runner.on('fail', (t) => console.log('MOCHA_RESULT FAIL ' + id(t)));
    runner.on('pending', (t) => console.log('MOCHA_RESULT SKIP ' + id(t)));
}

module.exports = MochaIds;
"""


def _run_script(repo: str, apply: str) -> str:
    return f"""#!/bin/bash
set -o pipefail

cd /home/{repo}
{apply}
npm test -- --reporter /home/mocha_ids_reporter.cjs

"""


class PlaycanvasEngineImageBase(Image):
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
        return "node:24-bookworm"

    def image_tag(self) -> str:
        return "base-node24"

    def workdir(self) -> str:
        return "base-node24"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""FROM {self.dependency()}
{self.global_env}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates python3 build-essential pkg-config \\
    libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /home
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN npm ci --no-audit --no-fund || true
{self.clear_env}
"""


class PlaycanvasEngineImageDefault(Image):
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
        return PlaycanvasEngineImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "mocha_ids_reporter.cjs", REPORTER),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {self.pr.base.sha}
git clean -fdx -e node_modules
npm ci --no-audit --no-fund

""",
            ),
            File(".", "run.sh", _run_script(repo, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(repo, "git apply --whitespace=nowarn /home/test.patch || exit 1"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || exit 1",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {dep.image_name()}:{dep.image_tag()}
{self.global_env}
{copy_commands}
RUN bash /home/prepare.sh
{self.clear_env}
"""


@Instance.register("playcanvas", "engine_9489_to_9404")
@Instance.register("playcanvas", "engine")
class PlaycanvasEngine9489To9404(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PlaycanvasEngineImageDefault(self.pr, self._config)

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
        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        result = re.compile(r"^MOCHA_RESULT (PASS|FAIL|SKIP) (.+?)\s*$")
        for line in ansi.sub("", test_log).splitlines():
            m = result.match(line.strip())
            if not m:
                continue
            status, name = m.groups()
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)
        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
