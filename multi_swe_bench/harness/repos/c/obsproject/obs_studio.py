from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.c.obsproject.obs_studio_0_to_1999 import (
    OBS_STUDIO_0_TO_1999,
    OBS_STUDIO_0_TO_1999_ImageBase,
    OBS_STUDIO_0_TO_1999_ImageDefault,
)


@Instance.register("obsproject", "obs-studio")
class OBS_STUDIO(OBS_STUDIO_0_TO_1999):
    pass
