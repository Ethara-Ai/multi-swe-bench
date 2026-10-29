from multi_swe_bench.harness.repos.golang.goharbor.harbor import *  # noqa: F401,F403
from multi_swe_bench.harness.repos.golang.goharbor.harbor_23925_to_24028 import *

# Imported AFTER harbor.py on purpose: the era adapter takes over the bare
# "goharbor/harbor" registry key and captures the previous holder at import time
# so PRs outside its interval keep routing to the legacy config.
from multi_swe_bench.harness.repos.golang.goharbor.harbor_23865_to_23078 import *  # noqa: F401,F403
