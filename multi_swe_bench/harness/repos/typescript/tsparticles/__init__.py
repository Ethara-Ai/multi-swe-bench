from multi_swe_bench.harness.repos.typescript.tsparticles.tsparticles_1756_to_909 import *
from multi_swe_bench.harness.repos.typescript.tsparticles.tsparticles_4605_to_1803 import *
from multi_swe_bench.harness.repos.typescript.tsparticles.tsparticles_4912_to_4742 import *
from multi_swe_bench.harness.repos.typescript.tsparticles.tsparticles_5226_to_5226 import *


from multi_swe_bench.harness.instance import Instance as _Instance


@_Instance.register("tsparticles", "tsparticles")
class TSPARTICLES:
    """Fallback for lookups that drop ``number_interval`` (e.g. the report
    phase keys instances as ``tsparticles/tsparticles``). Routes by PR number
    to the matching era config."""

    _ERAS = (
        (1756, TSPARTICLES_1756_TO_909),
        (4605, TSPARTICLES_4605_TO_1803),
        (4912, TSPARTICLES_4912_TO_4742),
        (5226, TSPARTICLES_5226_TO_5226),
    )

    def __new__(cls, pr, config, *args, **kwargs):
        for hi, era_cls in cls._ERAS:
            if pr.number <= hi:
                return era_cls(pr, config, *args, **kwargs)
        return cls._ERAS[-1][1](pr, config, *args, **kwargs)
