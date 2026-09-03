"""Era dispatcher for the bare `webdriverio/webdriverio` registration key.

WHY THIS EXISTS
---------------
`Instance.create()` builds its lookup key from the JSONL record (instance.py:40-51):

    if number_interval: key = f"{org}/{number_interval}"
    elif not tag:       key = f"{org}/{repo}"

A raw dataset that carries no `number_interval` therefore asks for
`webdriverio/webdriverio` regardless of which era its PR belongs to. Before this
module, that key resolved to NOTHING:

  * `webdriverio.py` does register it, but the module never loads. The org
    package contains BOTH `webdriverio.py` and a duplicated sub-package
    `webdriverio/` (tracked in git, files byte-identical to their parents), so
    `from ...typescript.webdriverio.webdriverio import *` resolves to the
    PACKAGE, not the module -- Python finds packages before modules of the same
    name. `webdriverio.py` is shadowed and its registration never runs.
  * The two era modules register only their interval keys,
    `webdriverio_10978_to_5279` and `webdriverio_14635_to_8578`.

The visible symptom is the worst kind: `Instance.create()` raises
"Instance 'webdriverio/webdriverio' is not registered", `build_dataset` logs it
and SILENTLY SKIPS the instance -- no images, no report, no obvious error.

WHAT THIS DOES
--------------
Registers `webdriverio/webdriverio` and forwards every Instance call to the
era config that actually matches the PR number, so a dataset with no
`number_interval` still reaches the right toolchain.

    PR <= 10978   -> WebDriverIOJest      (node:18, npm + lerna, jest)
    PR >  10978   -> WebDriverIOVitestNpm (node:20, npm, vitest)

The declared intervals overlap (5279-10978 and 8578-14635), so the boundary is
resolved in favour of the Jest era at and below 10978, which is where the
lerna-based monorepo layout still applies.

Verified against webdriverio/webdriverio#6000 (merged 2020-10-18, base
ac7d3256). At that commit the repo has `package-lock.json`, `.nvmrc` pinning
`lts/dubnium`, a lerna monorepo, and Jest tests -- matching the Jest era
exactly. The shadowed `webdriverio.py` targets node:22 with pnpm and runs
`pnpm run test:unit:run`, a script that does not exist at that commit, so it
would have failed all three stages with "Missing script".

SCOPE AND LIMITS
----------------
This module changes nothing that previously worked: the key it claims was
unregistered, so no existing routing is affected. It does not touch the era
configs, and it does not remove the duplicated sub-package.

PRs above 14635 (the modern pnpm era that `webdriverio.py` was written for)
land on the Vitest era here, because the pnpm config remains unreachable while
the duplicate package shadows it. Deleting
`repos/typescript/webdriverio/webdriverio/` would unshadow `webdriverio.py` and
let it claim this key back -- at which point this dispatcher should either be
removed or imported after it in the org `__init__.py`. That duplicate is
committed upstream, so removing it is a separate decision.
"""

from __future__ import annotations

from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.typescript.webdriverio.webdriverio_10978_to_5279 import (
    WebDriverIOJest,
)
from multi_swe_bench.harness.repos.typescript.webdriverio.webdriverio_14635_to_8578 import (
    WebDriverIOVitestNpm,
)
from multi_swe_bench.harness.repos.typescript.webdriverio.webdriverio_pnpm import (
    WebDriverIOVitestPnpm,
)

# Toolchain boundaries on the MAIN branch, established by inspecting package.json,
# the lockfile and .nvmrc at every base commit in the dataset:
#
#   PR <= 8432    npm + lerna + Jest      (node:18)  -> WebDriverIOJest
#   8578..12092   npm + lerna + vitest    (node:20)  -> WebDriverIOVitestNpm
#   PR >= 12432   pnpm workspace + vitest (node:24)  -> WebDriverIOVitestPnpm
#
# jest is REMOVED at PR 8578 (no jest dep, no jest.config -- replaced by vitest),
# so the previous single boundary at 10978 misrouted every 8578..10978 PR to the
# dead Jest config. These boundaries fix that.
JEST_ERA_MAX_PR = 8432
VITEST_NPM_ERA_MAX_PR = 12092

# The v8 MAINTENANCE branch never migrated to pnpm: its backport PRs (e.g. 12778,
# 14235, 14617) keep npm + lerna + vitest even though their PR numbers interleave
# with the main-branch pnpm PRs. PR number alone cannot separate them, so these
# are routed by base.ref before the numeric rules below.
LEGACY_NPM_REFS = {"v8"}


class WebDriverIODispatch(Instance):
    """Forwards to whichever era config matches this PR.

    Composition rather than inheritance: the era classes are complete, working
    Instances, and delegating leaves them untouched. Every method of the
    Instance interface is forwarded, so the harness cannot tell the difference.

    Routing is by base.ref first (to catch the v8 npm backports whose numbers
    interleave with the pnpm era), then by PR number for the main-branch eras.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

        base_ref = getattr(pr.base, "ref", "") or ""
        if base_ref in LEGACY_NPM_REFS:
            era = WebDriverIOVitestNpm
        elif pr.number <= JEST_ERA_MAX_PR:
            era = WebDriverIOJest
        elif pr.number <= VITEST_NPM_ERA_MAX_PR:
            era = WebDriverIOVitestNpm
        else:
            era = WebDriverIOVitestPnpm
        self._delegate: Instance = era(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

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


# Registered last so that this claims the key. See SCOPE AND LIMITS above for
# what happens if `webdriverio.py` is ever unshadowed.
Instance.register("webdriverio", "webdriverio")(WebDriverIODispatch)
