import re
from typing import Optional

from multi_swe_bench.harness.image import Config, Image, SWEImageDefault
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult


@Instance.register("plotly", "plotly.py")
class Plotly(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SWEImageDefault(self.pr, self._config)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        test_status_map = {}
        escapes = "".join([chr(char) for char in range(1, 32)])
        for line in log.split("\n"):
            line = re.sub(r"\[(\d+)m", "", line)
            translator = str.maketrans("", "", escapes)
            line = line.translate(translator)
            if any([line.startswith(x.value) for x in TestStatus]):
                if line.startswith(TestStatus.FAILED.value):
                    line = line.replace(" - ", " ")
                test_case = line.split()
                if len(test_case) >= 2:
                    test_status_map[test_case[1]] = test_case[0]
            # Support older pytest versions by checking if the line ends with the test status
            elif any([line.endswith(x.value) for x in TestStatus]):
                test_case = line.split()
                if len(test_case) >= 2:
                    test_status_map[test_case[0]] = test_case[1]

        return mapping_to_testresult(test_status_map)


@Instance.register("plotly", "dash")
class DashGeneric(Instance):
    """Bare-key `plotly/dash` fallback used ONLY for report generation.

    Build/run always route on `number_interval` (the dash-joined bundle) to a
    per-era class, so this class is never hit there. But gen_report's
    `--mode dataset` path collects report tasks BEFORE it loads the raw dataset
    (run_evaluation() pre-loads via `_ = self.dataset`; run_dataset() does not),
    so each ReportTask carries an empty number_interval and
    ReportTask.instance -> Instance.create() resolves to the bare "plotly/dash"
    key. Without this registration every dash report errors with
    "Instance 'plotly/dash' is not registered" and 0 reports are produced.

    Report generation only ever calls parse_log(), and all four era configs share
    an identical pytest parser, so this single generic parser is correct for every
    dash bundle regardless of era. No image/run methods are needed here.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        # Not used during report generation; provided for interface completeness.
        return SWEImageDefault(self.pr, self._config)

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        pattern = r"(tests/[^:]+::[^\s]+)\s+(PASSED|FAILED|ERROR|SKIPPED)|(PASSED|FAILED|ERROR|SKIPPED)\s+(tests/[^:]+::[^\s]+)"
        for line in log.splitlines():
            match = re.search(pattern, line)
            if not match:
                continue
            test = match.group(1) or match.group(4)
            status = match.group(2) or match.group(3)
            if not (test and status):
                continue
            if status == "PASSED":
                passed_tests.add(test)
            elif status in ["FAILED", "ERROR"]:
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
