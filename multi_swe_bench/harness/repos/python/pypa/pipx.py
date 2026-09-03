from __future__ import annotations

"""pypa/pipx dispatcher — routes PRs to the correct era class.

The raw dataset leaves `number_interval` (and `tag`) empty on every record,
so at instance-creation time the harness falls back to `{org}/{repo}` =
`pypa/pipx` for registry lookup (see harness/instance.py:42-48). We register
this dispatcher under that key; it instantiates the correct era class per
PR number and delegates every Instance method to it.

Era boundaries (from pipx_541_to_340.py and pipx_1721_to_804.py):
    pr.number <= 541  -> PIPX_541_TO_340  (covers 340..541)
    pr.number >= 804  -> PIPX_1721_TO_804 (covers 804..1721)
    542..803          -> UNREGISTERED gap; explicit error (no PRs in the
                         current dataset fall here, but guard defensively so
                         a future PR in that range fails loud instead of
                         being silently misrouted).
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.python.pypa.pipx_541_to_340 import PIPX_541_TO_340
from multi_swe_bench.harness.repos.python.pypa.pipx_1721_to_804 import PIPX_1721_TO_804


def _select_era(pr: PullRequest, config: Config) -> Instance:
    n = pr.number
    if n <= 541:
        return PIPX_541_TO_340(pr, config)
    if n >= 804:
        return PIPX_1721_TO_804(pr, config)
    raise ValueError(
        f"pypa/pipx PR #{n} falls in the unregistered gap 542..803; "
        f"no era class covers it. Add a new era config if needed."
    )


@Instance.register("pypa", "pipx")
class Pipx(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._inner = _select_era(pr, config)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._inner.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._inner.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._inner.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._inner.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, log: str) -> TestResult:
        return self._inner.parse_log(log)


for _n in ("465", "1251", "1252", "1261", "1291"):
    Instance.register("pypa", _n)(Pipx)
