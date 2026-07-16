"""Routes plain ``lobehub/lobehub`` PRs to the correct era config.

The raw dataset (``lobehub__lobehub_lht_final.jsonl``) carries neither ``tag``
nor ``number_interval``, so ``Instance.create()`` resolves the registry name to
``f"{org}/{repo}"`` -> ``"lobehub/lobehub"``. Without an entry under that name
every PR is dropped with "Instance 'lobehub/lobehub' is not registered."

Registering this router under the plain name dispatches each PR to its era by
PR number, so the dataset needs no ``tag``/``number_interval`` injection:

    71   .. 6452   -> LOBEHUB_6452_TO_71    (single-app era, npm/pnpm + vitest)
    6474 .. 13716  -> LOBEHUB_13716_TO_6474 (pnpm monorepo era, vitest)
"""

from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_6452_to_71 import (
    LOBEHUB_6452_TO_71,
)
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_13716_to_6474 import (
    LOBEHUB_13716_TO_6474,
)

# First PR of the monorepo era; anything below this belongs to the early era.
LATE_ERA_MIN_PR = 6474


@Instance.register("lobehub", "lobehub")
class LobeHubRouter(Instance):
    """Dispatching shim: never instantiated itself.

    ``Instance.create`` calls ``registry[name](pr, config, ...)``, so returning a
    different class from ``__new__`` hands back a real era instance. Python skips
    ``__init__`` because the returned object is not an instance of this class.
    """

    def __new__(cls, pr, config, *args, **kwargs):
        if pr.number >= LATE_ERA_MIN_PR:
            return LOBEHUB_13716_TO_6474(pr, config, *args, **kwargs)
        return LOBEHUB_6452_TO_71(pr, config, *args, **kwargs)
