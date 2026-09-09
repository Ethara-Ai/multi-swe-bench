"""serverless/serverless -- assigned bundle 8913_to_8652 (10 PRs, JavaScript).

Registry key for this bundle, per the dataset assignment table:

    serverless/serverless | JavaScript | 10 | serverless_8913_to_8652

WHY THIS FILE EXISTS AND WHY IT IS THIN
--------------------------------------
The build/test machinery for these ten PRs already lives in `serverless.py` and
is correct for this era: all ten PR numbers (8652-8913) fall at or below
MOCHA_ERA_MAX_NUMBER (13185), so `Serverless.dependency()` already routes them to
`ServerlessMochaImageDefault` on the shared `base` image. Re-implementing that
here would be duplicated code that could drift out of sync with the parent for
no behavioural gain.

What was actually MISSING was the routing key. `Instance.create()` dispatches on
`pr.number_interval` (instance.py:41-49), and the raw dataset ships these ten PRs
with NO `number_interval` and NO `prs_in_bundle` -- which is exactly the case
`serverless.py`'s shim warns about at load time:

    serverless pr-8913: no number_interval and no prs_in_bundle;
    backfill from the source _lht_final.jsonl by PR number

With the field empty, routing fell through to the generic `serverless/serverless`
registration, so the bundle had no identity of its own. This file supplies that
backfill by PR number (the remedy the warning names) and registers the bundle
under both canonical keys.

The subclass is therefore a pure re-registration: identical images, identical
tags (`base`, `pr-<N>`), identical run scripts and parser. Only the registry key
and `number_interval` change.
"""

from __future__ import annotations

import json as _json
import logging as _logging
from typing import Optional

from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.serverless.serverless import Serverless

_log = _logging.getLogger(__name__)

# The ten PRs of this bundle, ascending. Taken from the raw dataset
# (serverless__serverless_raw_dataset.jsonl), not hand-typed from the table.
BUNDLE_PR_NUMBERS = [
    8652, 8655, 8698, 8710, 8784,
    8840, 8870, 8874, 8875, 8913,
]

# number_interval is the EXACT dash-joined PR list, never a range -- the same
# contract serverless.py enforces in _sls_is_valid_number_interval().
NUMBER_INTERVAL = "-".join(str(n) for n in BUNDLE_PR_NUMBERS)

# The human-facing bundle name from the assignment table: <hi>_to_<lo>.
ALIAS_KEY = "serverless_8913_to_8652"

_BUNDLE_SET = set(BUNDLE_PR_NUMBERS)


class Serverless8913To8652(Serverless):
    """Bundle 8913_to_8652. Behaviour inherited wholesale from Serverless."""


Instance.register("serverless", NUMBER_INTERVAL)(Serverless8913To8652)
Instance.register("serverless", ALIAS_KEY)(Serverless8913To8652)


# ---------------------------------------------------------------------------
# number_interval backfill by PR number.
#
# Chains onto the from_json shim serverless.py already installed (it runs first
# via __init__.py import order), so its prs_in_bundle handling still wins where
# that field exists. This only fills the gap it explicitly leaves open: a PR of
# THIS bundle that arrived with no interval at all.
#
# Deliberately narrow -- org/repo must match, the number must be one of the ten,
# and an existing value is never overwritten.
# ---------------------------------------------------------------------------
if not getattr(PullRequest, "_serverless_8913_to_8652_ni_shim", False):
    _prev_from_json = PullRequest.from_json.__func__

    def _from_json(cls, json_str):
        pr = _prev_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "serverless"
                and getattr(pr, "repo", "") == "serverless"
                and getattr(pr, "number", None) in _BUNDLE_SET
                and not (getattr(pr, "number_interval", "") or "")
            ):
                pr.number_interval = NUMBER_INTERVAL
                _log.debug(
                    "serverless pr-%s: backfilled number_interval for bundle %s",
                    pr.number, ALIAS_KEY,
                )
        except Exception:
            _log.debug("number_interval backfill failed", exc_info=True)
        return pr

    PullRequest.from_json = classmethod(_from_json)
    PullRequest._serverless_8913_to_8652_ni_shim = True
