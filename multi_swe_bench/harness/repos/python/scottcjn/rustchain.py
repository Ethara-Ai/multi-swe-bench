import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "python:3.10-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return []

    def extra_setup(self) -> str:
        return "RUN pip install --no-cache-dir pytest requests flask"

    def dockerfile(self) -> str:
        base = super().dockerfile()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return base.replace(
            'CMD ["/bin/bash"]',
            f'{copy_commands}\nRUN bash /home/prepare.sh\n\nCMD ["/bin/bash"]',
        )

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
ls -F
###ACTION_DELIMITER###
pip install pytest requests flask
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors || true
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
pytest --no-header -rA --tb=no -p no:cacheprovider --ignore=deprecated/ --override-ini="testpaths=." --continue-on-collection-errors
""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("Scottcjn", "Rustchain")
class RUSTCHAIN(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}

        pattern = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?)\s+(\S+)")
        for line in test_log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_name = match.group(1), match.group(2)
                test_name = test_name.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
