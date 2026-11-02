from multi_swe_bench.harness.repos.javascript.postcss.autoprefixer import *

# Era config for autoprefixer PR 1560 (uvu + pnpm). It registers only its own
# interval key ("postcss/autoprefixer_1560_to_1560"); the record reaches it through
# its JSONL `number_interval`, so autoprefixer.py and the bare
# "postcss/autoprefixer" key are untouched.
from multi_swe_bench.harness.repos.javascript.postcss.autoprefixer_1560_to_1560 import *  # noqa: F401,F403
