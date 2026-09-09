"""Era routing for `mattermost/mattermost`.

The two era classes in this package register under synthetic repo names
(`mattermost/mattermost_0_to_21999`, `mattermost/mattermost_go_22000_to_99999`),
but raw dataset records carry `repo: "mattermost"` with no `tag` or
`number_interval`. `Instance.create` therefore looks up `mattermost/mattermost`,
which nothing registers, and every instance fails with
"Instance 'mattermost/mattermost' is not registered."

This mirrors the routing hook the handsontable config uses: wrap
`Instance.create` so a bare `mattermost/mattermost` record is dispatched to the
era whose lower bound its PR number clears. An explicit `number_interval` or
`tag` still wins, and every other repo falls straight through to the previous
implementation -- the wrapper chains rather than replaces, so it composes with
other repos' hooks regardless of import order.
"""

from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "mattermost"
REPO = "mattermost"

# (lower_bound_inclusive, registered_era_key), highest bound first.
_ERAS = [
    (22000, "mattermost_go_22000_to_99999"),
    (0, "mattermost_0_to_21999"),
]


_WEBAPP_ERA = "mattermost_webapp"

_GO_SUFFIXES = (".go",)
_WEBAPP_PREFIXES = ("webapp/",)


def _patch_paths(pr: PullRequest) -> set:
    """Every path named by a `diff --git a/<path>` header in either patch."""
    import re

    paths = set()
    for patch in (getattr(pr, "test_patch", "") or "", getattr(pr, "fix_patch", "") or ""):
        paths |= set(re.findall(r"^diff --git a/(\S+)", patch, re.M))
    return paths


def resolve_era(pr: PullRequest) -> str:
    """Pick the era from what the patches actually touch, not the PR number.

    mattermost/mattermost is a monorepo. Routing purely by number sends webapp
    PRs to the Go eras, whose scripts run `go test ./...` and therefore cannot
    observe a TypeScript change: run/test/fix come back byte-identical, f2p is
    empty, and the instance is rejected as invalid. Dispatch on file extension
    instead, and only fall back to the number-based Go eras when the patches say
    nothing useful.
    """
    paths = _patch_paths(pr)
    has_go = any(p.endswith(_GO_SUFFIXES) for p in paths)
    has_webapp = any(p.startswith(_WEBAPP_PREFIXES) for p in paths)

    # Frontend-only -> the webapp/jest era.
    if has_webapp and not has_go:
        return _WEBAPP_ERA

    # Go present (or nothing recognisable) -> the number-keyed Go eras. A mixed
    # PR stays on Go: the server suite is the one that can regress a Go fix.
    number = getattr(pr, "number", 0) or 0
    for lower_bound, era_key in _ERAS:
        if number >= lower_bound:
            return era_key
    return ""


if not getattr(Instance, "_mattermost_route_hook", False):
    _prev_create = Instance.create.__func__

    def _mattermost_create(cls, pr, config, *args, **kwargs):
        if getattr(pr, "org", "") == ORG and getattr(pr, "repo", "") == REPO:
            if not getattr(pr, "number_interval", "") and not getattr(pr, "tag", ""):
                era_key = resolve_era(pr)
                if era_key and f"{pr.org}/{era_key}" in cls._registry:
                    return cls._registry[f"{pr.org}/{era_key}"](
                        pr, config, *args, **kwargs
                    )
        return _prev_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_mattermost_create)
    Instance._mattermost_route_hook = True
