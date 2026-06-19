import re
from typing import Optional

from multi_swe_bench.harness.image import Config, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Dispatcher for the bare "pre-commit/pre-commit" registry key.
#
# The era classes in this package register under number_interval keys
# (pre-commit_2602_to_459, ...). But the raw dataset (pre-commit__pre-commit.jsonl)
# ships records with an EMPTY number_interval and EMPTY tag, so Instance.create
# resolves them to "pre-commit/pre-commit" -- a name none of the era classes own.
# Without this dispatcher every such record raises
#   ValueError: Instance 'pre-commit/pre-commit' is not registered.
#
# This class claims that bare key and routes by pr.number to the correct era's
# already-conformed ImageDefault (string dependency -> hardened Image.dockerfile()
# with the git-history scrub). The run scripts / parse_log are identical across
# every era, so they live here once.
#
# The per-era membership lists below are copied verbatim from each era file's
# header comment; together they enumerate this repo's full PR universe. A number
# outside every list falls back by range to the nearest modern era.

from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_1004_to_1004 import (
    ImageDefault as _Img1004,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_1268_to_1027 import (
    ImageDefault as _Img1268,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_886_to_468 import (
    ImageDefault as _Img886,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_956_to_916 import (
    ImageDefault as _Img956,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_2602_to_459 import (
    ImageDefault as _Img2602,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_3510_to_2713 import (
    ImageDefault as _Img3510,
)
from multi_swe_bench.harness.repos.python.pre_commit.pre_commit_3586_to_3578 import (
    ImageDefault as _Img3586,
)

# PR number -> era ImageDefault, built from each era file's explicit list.
_ERA_LISTS = [
    (_Img1004, [1004]),  # python:2.7-slim-buster
    (_Img1268, [1027, 1054, 1116, 1124, 1195, 1200, 1268]),  # python:2.7-slim-buster
    (
        _Img886,
        [
            468, 474, 479, 496, 501, 515, 552, 553, 559, 584, 601, 602, 604,
            614, 616, 619, 626, 639, 678, 684, 690, 694, 700, 716, 724, 739,
            751, 759, 769, 779, 805, 811, 837, 839, 845, 886,
        ],
    ),  # python:3.5-slim-stretch
    (_Img956, [916, 956]),  # python:3.7-slim-buster
    (
        _Img2602,
        [
            459, 462, 1303, 1339, 1371, 1413, 1450, 1506, 1531, 1572, 1590,
            1677, 1707, 1714, 1715, 1717, 1781, 1792, 1839, 1841, 1919, 2004,
            2027, 2039, 2154, 2215, 2329, 2384, 2455, 2602,
        ],
    ),  # python:3.8-slim-bookworm
    (
        _Img3510,
        [
            2713, 2725, 2726, 2729, 2843, 2879, 2889, 2908, 2991, 3033, 3102,
            3122, 3169, 3199, 3207, 3304, 3323, 3390, 3439, 3510,
        ],
    ),  # python:3.9-slim-bookworm
    (_Img3586, [3578, 3586]),  # python:3.10-slim-bookworm
]

_PR_ERA: dict[int, type] = {}
for _img, _nums in _ERA_LISTS:
    for _n in _nums:
        _PR_ERA[_n] = _img


def _pick_image_class(number: int) -> type:
    if number in _PR_ERA:
        return _PR_ERA[number]
    # Fallback for a PR number outside the recorded lists: route by range to the
    # nearest modern (bookworm) era so the build still succeeds.
    if number >= 3578:
        return _Img3586  # 3.10
    if number >= 2713:
        return _Img3510  # 3.9
    return _Img2602  # 3.8 — broad mid-range default


@Instance.register("pre-commit", "pre-commit")
class PreCommitDispatch(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _pick_image_class(self.pr.number)(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Verbose pytest output: tests/foo/bar.py::test_name PASSED/FAILED/SKIPPED
        verbose_re = re.compile(
            r"^(tests/.*?::\S+)\s+(PASSED|FAILED|SKIPPED)", re.MULTILINE
        )
        for match in verbose_re.finditer(log):
            test_name, status = match.groups()
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Short test summary (-rA shows PASSED/FAILED/SKIPPED/ERROR)
        summary_re = re.compile(
            r"^=+\s+short test summary info\s+=+((?:.|\n)*?)^=+.+=$", re.MULTILINE
        )
        summary_match = summary_re.search(log)
        if summary_match:
            summary_content = summary_match.group(1)
            failed_re = re.compile(r"^(?:FAILED|ERROR) (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in failed_re.finditer(summary_content):
                failed_tests.add(match.group(1).strip())
            passed_re = re.compile(r"^PASSED (.*?)$", re.MULTILINE)
            for match in passed_re.finditer(summary_content):
                passed_tests.add(match.group(1).strip())
            skipped_re = re.compile(r"^SKIPPED (.*?)(?:\ - .*)?$", re.MULTILINE)
            for match in skipped_re.finditer(summary_content):
                skipped_tests.add(match.group(1).strip())

        passed_tests.difference_update(failed_tests)
        skipped_tests.difference_update(failed_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
