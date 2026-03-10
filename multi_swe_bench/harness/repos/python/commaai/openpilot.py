import re
from typing import Optional

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
        return "ubuntu:24.04"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return [
            "python3",
            "python3-pip",
            "python3-venv",
            "python3-dev",
            "libffi-dev",
            "libssl-dev",
            "libcurl4-openssl-dev",
            "locales",
            "clang",
            "cmake",
            "pkg-config",
            "xvfb",
        ]

    def extra_setup(self) -> str:
        return (
            "RUN pip3 install --no-cache-dir --break-system-packages "
            "pytest scons cython numpy pycapnp setuptools cffi requests\n"
            "RUN git submodule update --init --recursive || true\n"
            "RUN scons -j$(nproc) || true"
        )

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
                """ls -F
###ACTION_DELIMITER###
pip3 install --break-system-packages pytest scons cython numpy pycapnp
###ACTION_DELIMITER###
git submodule update --init --recursive || true
###ACTION_DELIMITER###
scons -j$(nproc) || true
###ACTION_DELIMITER###
pytest --no-header -rA --tb=no -p no:cacheprovider
###ACTION_DELIMITER###
echo 'pytest --no-header -rA --tb=no -p no:cacheprovider' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
pytest --no-header -rA --tb=no -p no:cacheprovider -o "addopts="

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest --no-header -rA --tb=no -p no:cacheprovider -o "addopts="

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest --no-header -rA --tb=no -p no:cacheprovider -o "addopts="

""".format(repo=self.pr.repo),
            ),
        ]


@Instance.register("commaai", "openpilot")
class OPENPILOT(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}

        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?)\s+([^:\s]+)"
        )
        for line in log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_file = match.group(1), match.group(2)
                test_file = test_file.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_file] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_file) != "failed":
                        test_results[test_file] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_file) not in ["failed", "skipped"]:
                        test_results[test_file] = "passed"

        for test_file, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_file)
            elif status == "failed":
                failed_tests.add(test_file)
            elif status == "skipped":
                skipped_tests.add(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
