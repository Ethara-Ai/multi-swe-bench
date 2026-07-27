import json as _json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
import multi_swe_bench.harness.pull_request as _pull_request

# ---------------------------------------------------------------------------
# Emit `number_interval` on the OUTPUT (resolved jsonl) rows for processing/p5.js.
#
# Each instance is a release-delta BUNDLE. The raw record carries `prs_in_bundle`
# (e.g. [1938, 1944]) but an EMPTY `number_interval`. The required output format
# is the dash-JOINED bundle list -- the explicit PR numbers, NOT a min-max range:
# [146, 147, 150, 155, 157] -> "146-147-150-155-157" (a "146-157" range would
# wrongly imply every PR in between is part of the bundle).
#
# Two constraints force the approach below:
#   * `prs_in_bundle` is NOT a PullRequest field, so the dataclass-json loader
#     DROPS it -- the registry classes never see it.
#   * Setting `pr.number_interval` during load would change the ROUTING key
#     (instance.py: name becomes "processing/1938-1944"), which is not
#     registered -> instance creation fails; routing must stay "processing/p5.js".
#
# So, following the aquasecurity/tfsec + netbirdio/netbird convention, two
# import-time monkeypatches SCOPED TO THIS REGISTRY (no edits to harness source):
#   1. PullRequest.from_json -- re-read the raw json and stash the dash-joined
#      value in a NON-field attr `_p5js_number_interval` (routing key stays "").
#   2. Dataset.build -- stamp `ds.number_interval` from that stash onto the
#      OUTPUT row only. gen_report builds every resolved-jsonl row via
#      Dataset.build(raw_dataset[id], report), so the output then carries it.
if not getattr(_pull_request.PullRequest, "_p5js_number_interval_patched", False):
    _p5js_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _p5js_from_json(cls, json_str):
        pr = _p5js_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "processing"
                and raw.get("repo") == "p5.js"
                and raw.get("prs_in_bundle")
            ):
                # Stash only -- do NOT set pr.number_interval (the routing key).
                pr._p5js_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_p5js_from_json)
    _pull_request.PullRequest._p5js_number_interval_patched = True

    # Stamp number_interval onto the OUTPUT row only.
    # NOTE: Dataset subclasses PullRequest, so it INHERITS the flag above; use a
    # distinct flag and check the class's OWN __dict__ (not getattr, which would
    # see the inherited PullRequest flag and wrongly skip this patch).
    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_p5js_build_patched", False):
        _p5js_orig_build = _Dataset.build.__func__

        def _p5js_build(cls, pr, report):
            ds = _p5js_orig_build(cls, pr, report)
            ni = getattr(pr, "_p5js_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_p5js_build)
        _Dataset._p5js_build_patched = True
# ---------------------------------------------------------------------------

from multi_swe_bench.harness.repos.javascript.processing.p5_js_3089_to_1812 import (
    P5JS_3089_to_1812,
)
from multi_swe_bench.harness.repos.javascript.processing.p5_js_8679_to_3139 import (
    P5JS_8679_to_3139,
)
from multi_swe_bench.harness.repos.javascript.processing.p5_js_8476_to_7653 import (
    P5JS_8476_to_7653,
)


# ---------------------------------------------------------------------------
# Era routing
#
# The p5.js dataset carries no `number_interval` and no `tag`, so every record
# resolves through Instance.create() to the bare key "processing/p5.js" (see
# instance.py). The three per-era registrations (`p5.js_3089_to_1812`, etc.)
# are therefore never reached on their own. This module registers that single
# bare key and dispatches to the correct era build/test toolchain.
#
# Dispatch cannot key on PR number alone: the grunt (1.x) and vitest (2.x) eras
# interleave by number (e.g. PR 8679 is v1.11.x -> grunt, PR 8476 is v2.2.x ->
# vitest). The reliable signal is the release version, read from `base.label`
# (format "<base_tag>..<head_tag>", e.g. "0.7.0..0.7.1", "v2.2.1..v2.2.2").
#
#   era1a  ->  p5.js 0.5-0.7   node:14 + puppeteer/mocha (test/test.html runner)
#   era1b  ->  p5.js 0.8-1.11  node:20 + grunt connect/mochaChrome
#   era2   ->  p5.js 2.x       node:20 + vitest browser (playwright/chromium)
#
# This reproduces the original era ranges exactly:
#   era1a == numbers 1812..3089, era1b == 3139..8679 (minus 2.x), era2 == 2.x.
# ---------------------------------------------------------------------------


def _base_version(label: str) -> Optional[tuple[int, int]]:
    """(major, minor) of the base (left) tag in a "<base>..<head>" base.label.

    Tolerates a leading 'v' and missing patch component. Returns None if the
    label does not start with a parseable version.
    """
    left = (label or "").split("..")[0].strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)", left)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def _era_of(pr: PullRequest) -> str:
    ver = _base_version(pr.base.label)
    ref = (pr.base.ref or "").lower()

    # era2: the p5.js 2.x rewrite (vitest). Detected by the base major version,
    # with the `dev-2.x` release branch as a fallback signal. This is the only
    # boundary where PR numbers interleave with era1b, so version must decide it.
    if (ver is not None and ver[0] >= 2) or ref.startswith("dev-2"):
        return "era2"

    # era1a vs era1b: grunt's mochaChrome runner replaced the old puppeteer
    # test/test.html harness around p5.js 0.7.3/0.8.0. Version at that boundary
    # is ambiguous (0.7.2 is era1a but 0.7.3 is already era1b), whereas PR
    # numbers are clean and non-overlapping: era1a <= 3089, era1b >= 3139.
    return "era1a" if int(pr.number) <= 3100 else "era1b"


_ERA_IMPL = {
    "era1a": P5JS_3089_to_1812,
    "era1b": P5JS_8679_to_3139,
    "era2": P5JS_8476_to_7653,
}


@Instance.register("processing", "p5.js")
class P5JS(Instance):
    """Bare-key entry point that delegates to the correct per-era instance."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._era = _era_of(pr)
        self._impl = _ERA_IMPL[self._era](pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return self._impl.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._impl.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._impl.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._impl.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._impl.parse_log(test_log)
