"""Dispatcher for babel/babel -- routes a PR number to its era config.

Why a dispatcher is needed
--------------------------
The raw dataset carries no ``number_interval`` and no ``tag``, so
``Instance.create`` (instance.py:40-51) resolves the registration key as
``org/repo``, i.e. ``babel/babel``. Every existing babel era registers under a
*different* key -- ``babel/babel_classic_jest``, ``babel/12695``,
``babel/13905``, ``babel/16692``, ``babel/13214-13229-13294``,
``babel/babel_classic_mocha``, ``babel/babel_npm_mocha`` -- and none of them is
``babel/babel``. Without this class every instance raises
``ValueError: Instance 'babel/babel' is not registered`` and is skipped.

Why the routing table is deliberately narrow
--------------------------------------------
Babel's era ranges **overlap**, because the project maintained ``main``, ``7.x``
and ``next-8-dev`` concurrently::

    Era 1  npm_mocha      #319   - #5427
    Era 2  classic_mocha  #4892  - #7450     overlaps era 1
    Era 3  classic_jest   #7358  - #11973
    Era 4  berry_jest     #10853 - #13727    overlaps era 3
    Era 5  yarn3_jest     #11554 - #16101    overlaps eras 3 and 4
    Era 6  yarn4_jest     #15959 - #17938    overlaps era 5

So a PR number alone does **not** determine the toolchain: #11000 could be
yarn-classic+jest on one branch or yarn-berry on another. Only the base commit
answers it -- the presence of ``.yarnrc.yml``, a ``packageManager`` field, or
``mocha`` vs ``jest`` in devDependencies.

This table therefore routes only the interval that was actually verified
per-commit, and refuses everything else rather than guessing. Extending it means
doing the same check for the new range: read ``package.json`` and ``.yarnrc.yml``
at each base commit, confirm which era they match, then add an entry.

What was verified for 7358-10852
--------------------------------
All five PRs in the current dataset (10198, 10217, 10447, 10599, 10680) were
checked at their base commits and agree on every marker::

    jest ^24.8.0 / ^24.9.0     yarn.lock v1, no .yarnrc.yml
    no packageManager field    engines.node >= 6.9.0 < 13.0.0 / < 14.0.0
    scripts.test = make test   upstream CI: `make -j test-ci`, node_js: "12"

which is exactly era 3 (``babel_classic_jest``). The upper bound is set to
**10852**, one below where era 4 begins, so this dispatcher can never route a PR
into the ambiguous era-3/era-4 overlap.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# NOTE: the era-3 Instance class is named `babel_classic_jest` -- the same
# identifier as its module -- so this import shadows the module name locally.
# That is fine here (only the class is needed) but is why the import is aliased.
from multi_swe_bench.harness.repos.typescript.babel.babel_classic_jest import (
    babel_classic_jest as BabelClassicJestInstance,
)

# (low, high, cls, label) -- inclusive bounds. Only ranges whose toolchain was
# confirmed at the base commit appear here; see the module docstring.
_ERAS = [
    (7358, 10852, BabelClassicJestInstance, "babel_classic_jest"),
]


def select_era(number: int):
    """Return the era class whose interval contains ``number``.

    Raises ``ValueError`` naming the number, the configured intervals, and the
    reason a wider table would be unsafe -- babel's eras overlap, so routing by
    PR number outside a verified range can silently pick the wrong toolchain and
    grade against a runner the repo was not using on that branch.
    """
    for low, high, cls, _label in _ERAS:
        if low <= number <= high:
            return cls

    known = ", ".join(f"{low}-{high}" for low, high, _c, _l in _ERAS)
    raise ValueError(
        f"babel/babel PR {number} falls outside every verified era ({known}). "
        f"Babel's era ranges overlap (era 3 is 7358-11973, era 4 is 10853-13727, "
        f"era 5 is 11554-16101), so the PR number alone cannot decide the "
        f"toolchain -- the same number can be yarn-classic+jest on one branch and "
        f"yarn-berry on another. Read package.json and .yarnrc.yml at this PR's "
        f"base commit, confirm which era it matches, then add an interval to "
        f"_ERAS."
    )


@Instance.register("babel", "babel")
class BABEL(Instance):
    """Thin forwarder to the era config that owns this PR number.

    Holds no build logic and duplicates none: every method delegates, so an era
    can change without this file moving. A dataset regenerated with
    ``number_interval`` set reaches the era classes directly and bypasses this
    class entirely, so keeping it costs nothing.
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
