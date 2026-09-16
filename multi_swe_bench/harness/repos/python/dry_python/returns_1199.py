from __future__ import annotations

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.python.dry_python.returns import (
    ImageBase,
    ImageDefault,
    parse_pytest_log,
)

NUMBER_INTERVAL = "returns_1199_to_1199"

_PYTEST = (
    'pytest -v --tb=short --override-ini="addopts=" '
    "-p no:hypothesispytest --mypy-ini-file=setup.cfg -p no:randomly "
    "--continue-on-collection-errors "
    "tests/ typesafety/"
)


class Returns1199ImageDefault(ImageDefault):
    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def files(self) -> list[File]:
        out = []
        for f in super().files():
            if f.name in ("run.sh", "test-run.sh", "fix-run.sh"):
                lines = []
                for line in f.content.splitlines():
                    if line.startswith("pytest "):
                        lines.append(_PYTEST)
                    else:
                        lines.append(line)
                out.append(File(f.dir, f.name, "\n".join(lines) + "\n"))
            else:
                out.append(f)
        return out


@Instance.register("dry-python", NUMBER_INTERVAL)
class Returns1199(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Returns1199ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        return parse_pytest_log(log)
