"""Era dispatcher for koxudaxi/datamodel-code-generator.

The three era configs in this directory are each self-contained and register
themselves under their own keys. This module adds a single registration on the
plain ``org/repo`` key so a dataset entry that carries no ``number_interval``
(and no ``tag``) still resolves: ``Instance.create()`` falls back to
``koxudaxi/datamodel-code-generator``, lands here, and is routed to the correct
era by PR number.

Adding a new era means adding one row to ``ERAS`` and one import.
"""

from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.python.koxudaxi.datamodel_code_generator_103_to_87 import (
    DATAMODEL_CODE_GENERATOR_103_TO_87,
)
from multi_swe_bench.harness.repos.python.koxudaxi.datamodel_code_generator_2242_to_1068 import (
    DATAMODEL_CODE_GENERATOR_2242_TO_1068,
)
from multi_swe_bench.harness.repos.python.koxudaxi.datamodel_code_generator_3020_to_2327 import (
    DATAMODEL_CODE_GENERATOR_3020_TO_2327,
)

# (lowest PR, highest PR, era class) — bounds inclusive, newest era first.
# Boundaries mirror the era filenames; the gaps between them are deliberate,
# they are PR ranges no era has been written for yet.
ERAS = [
    (2327, 3020, DATAMODEL_CODE_GENERATOR_3020_TO_2327),
    (1068, 2242, DATAMODEL_CODE_GENERATOR_2242_TO_1068),
    (87, 103, DATAMODEL_CODE_GENERATOR_103_TO_87),
]


def era_for(number: int):
    """Return the era class covering this PR number, or None if unrouted."""
    for low, high, era in ERAS:
        if low <= number <= high:
            return era
    return None


@Instance.register("koxudaxi", "datamodel-code-generator")
class DATAMODEL_CODE_GENERATOR(Instance):
    def __new__(cls, pr, config, *args, **kwargs):
        era = era_for(pr.number)
        if era is None:
            covered = ", ".join(f"{low}-{high}" for low, high, _ in ERAS)
            raise ValueError(
                f"koxudaxi/datamodel-code-generator PR #{pr.number} falls outside "
                f"every configured era (covered: {covered}). Write an era config "
                f"for it, or drop the PR from the dataset."
            )
        # Era classes are not subclasses of this one, so Python skips
        # __init__ here — the era's own __init__ has already run.
        return era(pr, config, *args, **kwargs)
