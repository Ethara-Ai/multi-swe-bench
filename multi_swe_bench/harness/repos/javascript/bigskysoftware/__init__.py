from multi_swe_bench.harness.repos.javascript.bigskysoftware.htmx import *

# Era config for htmx 4 (branch four-dev) PR 3828: web-test-runner + Playwright with
# the repo's own test/web-test-runner.config.mjs. It registers only its own interval
# key ("bigskysoftware/htmx_3828_to_3828"); the record reaches it through its JSONL
# `number_interval`, so htmx.py and the bare "bigskysoftware/htmx" key are untouched.
from multi_swe_bench.harness.repos.javascript.bigskysoftware.htmx_3828_to_3828 import *  # noqa: F401,F403
