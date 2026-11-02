from multi_swe_bench.harness.repos.javascript.openlayers.openlayers import *

# Era configs for PRs 17525..17640, one self-contained file per toolchain era. Each
# registers only its own interval key ("openlayers/<filename stem>"); records reach
# it through their JSONL `number_interval`, so openlayers.py and the bare
# "openlayers/openlayers" key are untouched.
from multi_swe_bench.harness.repos.javascript.openlayers.openlayers_17543_to_17525 import *  # noqa: F401,F403
from multi_swe_bench.harness.repos.javascript.openlayers.openlayers_17576_to_17576 import *  # noqa: F401,F403
from multi_swe_bench.harness.repos.javascript.openlayers.openlayers_17640_to_17591 import *  # noqa: F401,F403
