from multi_swe_bench.harness.repos.golang.jesseduffield.lazydocker import *
from multi_swe_bench.harness.repos.golang.jesseduffield.lazygit import *
from multi_swe_bench.harness.repos.golang.jesseduffield.lazygit_5487_to_12 import *

# Imported AFTER lazygit.py on purpose: the era adapter takes over the bare
# "jesseduffield/lazygit" registry key and captures the previous holder at import
# time so PRs outside its interval keep routing to the legacy config.
from multi_swe_bench.harness.repos.golang.jesseduffield.lazygit_5758_to_5660 import *  # noqa: F401,F403
