from multi_swe_bench.harness.repos.rust.starship.starship import *
from multi_swe_bench.harness.repos.rust.starship.starship_1336_to_85 import *
from multi_swe_bench.harness.repos.rust.starship.starship_mid_0x import *

# ---------------------------------------------------------------------------
# LHT bundle routing shim (radare2/tailwindcss pattern).
#
# The dataset stores prs_in_bundle but no number_interval. Instance.create()
# routes on f"{org}/{number_interval}", and each era file above has registered
# every bundle's dash-joined prs_in_bundle key against its toolchain era class
# (rust 1.47 / 1.56 / latest). This shim derives number_interval from
# prs_in_bundle at load time so those registrations are actually hit — without
# editing the dataset. The create() fallback keeps any un-registered bundle
# from hard-failing (routes it to the default rust:latest era).
# ---------------------------------------------------------------------------
import json as _st_json
from multi_swe_bench.harness.instance import Instance as _StInstance
from multi_swe_bench.harness.pull_request import PullRequest as _StPullRequest

if not getattr(_StPullRequest, "_starship_ni_shim", False):
    _st_orig_from_json = _StPullRequest.from_json.__func__

    def _st_from_json(cls, json_str):
        pr = _st_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == "starship"
                and getattr(pr, "repo", "") == "starship"
                and not getattr(pr, "number_interval", "")
            ):
                prs = (_st_json.loads(json_str) or {}).get("prs_in_bundle") or []
                if prs:
                    pr.number_interval = "-".join(str(p) for p in prs)
        except Exception:
            pass
        return pr

    _StPullRequest.from_json = classmethod(_st_from_json)
    _StPullRequest._starship_ni_shim = True

if not getattr(_StInstance, "_starship_route_shim", False):
    _st_orig_create = _StInstance.create.__func__

    def _st_create(cls, pr, config, *args, **kwargs):
        try:
            return _st_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == "starship" and getattr(pr, "repo", "") == "starship":
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    _StInstance.create = classmethod(_st_create)
    _StInstance._starship_route_shim = True
