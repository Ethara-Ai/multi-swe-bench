import re
from typing import Any

from multi_swe_bench.harness.image import Config
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

_NAME_RE = re.compile(r"^ckeditor/ckeditor5_(?P<hi>\d+)_to_(?P<lo>\d+)$")


def _intervals() -> list[tuple[int, int, type]]:
    found: list[tuple[int, int, type]] = []
    for name, impl in Instance._registry.items():
        match = _NAME_RE.match(name)
        if match:
            found.append((int(match.group("lo")), int(match.group("hi")), impl))
    return found


def _unroutable(number: int, covered: str) -> None:
    raise ValueError(
        f"ckeditor/ckeditor5 PR {number} falls outside every registered "
        f"interval ({covered}). Add an interval adapter that covers it, or set "
        f"number_interval on the record explicitly."
    )


@Instance.register("ckeditor", "ckeditor5")
class CKEDITOR5_DISPATCH(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args: Any, **kwargs: Any):
        intervals = _intervals()
        owners = [impl for low, high, impl in intervals if low <= pr.number <= high]
        if not owners:
            covered = ", ".join(
                f"{low}..{high}"
                for low, high in sorted((lo, hi) for lo, hi, _ in intervals)
            )
            _unroutable(pr.number, covered)
        return owners[0](pr, config, *args, **kwargs)
