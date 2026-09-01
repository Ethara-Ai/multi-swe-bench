"""Dispatcher for zwave-js/zwave-js -- routes a PR number to its era config.

Why a dispatcher is needed here
-------------------------------
The raw dataset carries ``number_interval: None`` and ``tag: None`` for all
five instances, so the harness resolves the registration key as ``org/repo``,
i.e. ``zwave-js/zwave-js``. That is a *single* key for a repo that has two
mutually incompatible toolchains, so one registered class has to decide which
one applies. Were the dataset later regenerated with ``number_interval`` set,
the harness would look up ``org/interval`` and reach the era classes directly;
this class never runs then, so keeping it costs nothing.

The eras
--------
=============  ==========  =======================================  ==========
PRs            runner      toolchain                                node
=============  ==========  =======================================  ==========
1167 - 5092    jest 26     yarn 1 / npm 6 + lerna 3, no build       14.x
5093 - 5460    *mixed*     -- refused, see below --                 --
5461 +         ava 4       yarn 3.5.0 (pnpm linker) + turbo, build  18
=============  ==========  =======================================  ==========

Why 5093-5460 is refused rather than guessed
--------------------------------------------
zwave-js migrated jest -> ava **one package at a time** over five months::

    #5093  2022-09-20  shared          #5443  2023-02-09  transformers
    #5096  2022-09-21  core            #5452  2023-02-10  config
    #5099  2022-09-22  nvmedit         #5460  2023-02-14  zwave-js (deletes jest.config.js)

Inside that window the monorepo is genuinely split: some packages answer to
ava, the rest still to jest. Either era config would run one runner and see
only the packages that had (or had not yet) migrated -- silently grading a
fraction of the repo and reporting a plausible-looking but wrong ``p2p``.
Raising here makes that a loud, named failure instead.

Extending it is a small, evidence-based edit: find which packages the PR
touches, confirm their migration state at that commit, and add an interval to
``_ERAS``. None of the five instances in the current dataset fall in the gap.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.typescript.zwave_js.zwave_js_5092_to_1167 import (
    ZWAVE_JS_5092_TO_1167,
)
from multi_swe_bench.harness.repos.typescript.zwave_js.zwave_js_99999_to_5461 import (
    ZWAVE_JS_99999_TO_5461,
)

# (low, high, cls, label) -- inclusive bounds, non-overlapping by construction.
# The deliberate hole between 5092 and 5461 is the mixed-runner window; see the
# module docstring.
_ERAS = [
    (1167, 5092, ZWAVE_JS_5092_TO_1167, "zwave_js_5092_to_1167"),
    (5461, 99999, ZWAVE_JS_99999_TO_5461, "zwave_js_99999_to_5461"),
]


def select_era(number: int):
    """Return the era class whose interval contains ``number``.

    Raises ``ValueError`` naming the number and every known interval when it
    falls outside them -- notably in the 5093-5460 mixed-runner window, where
    picking either era would grade only part of the monorepo.
    """
    for low, high, cls, _label in _ERAS:
        if low <= number <= high:
            return cls

    known = ", ".join(f"{low}-{high}" for low, high, _c, _l in _ERAS)
    raise ValueError(
        f"zwave-js/zwave-js PR {number} falls outside every configured era "
        f"({known}). PRs 5093-5460 are the jest->ava migration window, where "
        f"the monorepo runs both runners at once and neither era config would "
        f"see the whole test suite; add an interval to _ERAS only after "
        f"confirming which runner owns the packages this PR touches."
    )


@Instance.register("zwave-js", "zwave-js")
class ZWAVE_JS(Instance):
    """Thin forwarder to the era config that owns this PR number.

    Holds no build logic of its own and deliberately duplicates none: every
    method delegates, so an era can change freely without this file moving. A
    dataset regenerated with ``number_interval`` set bypasses this class
    entirely.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._delegate = select_era(pr.number)(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def delegate(self) -> Instance:
        return self._delegate

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._delegate.parse_log(test_log)
