from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_6452_to_71 import (
    LOBEHUB_6452_TO_71,
)
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_13716_to_6474 import (
    LOBEHUB_13716_TO_6474,
)
from multi_swe_bench.harness.repos.typescript.lobehub.lobehub_21367_to_17665 import (
    ERA_MIN_PR as MODERN_ERA_MIN_PR,
    LOBEHUB_21367_TO_17665,
)

LATE_ERA_MIN_PR = 6474


@Instance.register("lobehub", "lobehub")
class LobeHubRouter(Instance):
    def __new__(cls, pr, config, *args, **kwargs):
        if pr.number >= MODERN_ERA_MIN_PR:
            return LOBEHUB_21367_TO_17665(pr, config, *args, **kwargs)
        if pr.number >= LATE_ERA_MIN_PR:
            return LOBEHUB_13716_TO_6474(pr, config, *args, **kwargs)
        return LOBEHUB_6452_TO_71(pr, config, *args, **kwargs)
