from multi_swe_bench.harness.repos.javascript.prettier.prettier import *

# Imported AFTER prettier.py on purpose: the era adapter takes over the bare
# "prettier/prettier" registry key and captures the previous holder at import
# time so PRs outside its interval keep routing to the legacy config.
from multi_swe_bench.harness.repos.javascript.prettier.prettier_20318_to_19465 import *  # noqa: F401,F403
