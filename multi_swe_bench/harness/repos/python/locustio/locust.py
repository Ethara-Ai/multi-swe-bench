from typing import Optional

from multi_swe_bench.harness.image import Image
from multi_swe_bench.harness.instance import Instance, TestResult

from multi_swe_bench.harness.repos.python.locustio.locust_0_to_1790 import (
    ImageDefault as ImageDefault0To1790,
)
from multi_swe_bench.harness.repos.python.locustio.locust_1791_to_99999 import (
    ImageDefault as ImageDefault1791Plus,
    LOCUST_1791_TO_99999,
)


@Instance.register("locustio", "locust")
class LOCUST(LOCUST_1791_TO_99999):
    """Bare-name handler for `locustio/locust`.

    Datasets whose PRs carry no ``number_interval`` resolve to ``org/repo``
    (see Instance.create), so a bare registration is required in addition to
    the ranged ``locust_<lo>_to_<hi>`` instances. This dispatches to the same
    range-specific image config the ranged instances use, keyed by PR number,
    and inherits run/test/fix commands and parse_log from the ranged class.
    """

    def dependency(self) -> Optional[Image]:
        if self._pr.number <= 1790:
            return ImageDefault0To1790(self.pr, self._config)
        return ImageDefault1791Plus(self.pr, self._config)
