from multi_swe_bench.harness.repos.javascript.Automattic.mongoose import *

# Era config for mongoose PRs 16244..16582 (mocha 12 + tstyche 7, no lockfile). It
# registers only its own interval key ("Automattic/mongoose_16582_to_16244"); records
# reach it through their JSONL `number_interval`, so mongoose.py and the bare
# "Automattic/mongoose" key are untouched.
from multi_swe_bench.harness.repos.javascript.Automattic.mongoose_16582_to_16244 import *  # noqa: F401,F403
