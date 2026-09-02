from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.mem0ai.mem0 import (
    ImageBase,
    parse_pytest_log,
    pr_dockerfile,
    register_era,
)

register_era("mem0_1459_to_1459", bundles=("1459",))


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
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
git reset --hard
###ACTION_DELIMITER###
git clean -fd
###ACTION_DELIMITER###
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
test -z "$(git status --porcelain)"
###ACTION_DELIMITER###
bash /home/install-deps.sh
###ACTION_DELIMITER###
echo 'cd /home/{pr.repo}/embedchain && pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "install-deps.sh",
                """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
cd /home/{pr.repo}/embedchain && pip install --upgrade pip setuptools wheel poetry-core poetry || true
###ACTION_DELIMITER###
cd /home/{pr.repo}/embedchain && poetry install --all-extras 2>/dev/null || pip install -e ".[dev]" 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest pytest-mock pytest-env || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}/embedchain
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cd /home/{pr.repo}/embedchain
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
cd /home/{pr.repo}/embedchain
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register("mem0ai", "mem0_1459_to_1459")
class MEM0_1459_TO_1459(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_log(log)
