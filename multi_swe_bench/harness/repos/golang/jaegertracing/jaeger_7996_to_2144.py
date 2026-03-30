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
        return "golang:1.24"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

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
go mod download
###ACTION_DELIMITER###
go test -v ./...
###ACTION_DELIMITER###
echo "go test -v ./..." > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
go test -v ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
go test -v ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1  
fi
go test -v ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""\
# Jaeger Go modules era (PRs 2144-7996)
# go.mod based dependency management
FROM golang:1.24

# Install basic requirements
RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone https://github.com/jaegertracing/jaeger.git /home/jaeger

WORKDIR /home/jaeger
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
CMD ["bash"]
"""
        return dockerfile_content


@Instance.register("jaegertracing", "jaeger_7996_to_2144")
class JAEGER_7996_TO_2144(Instance):
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

    # Flaky TLS/gRPC tests that fail non-deterministically due to network
    # timing — not related to any PR's actual code changes.
    FLAKY_TEST_PREFIXES = (
        "TestServerGRPCTLS",
        "TestServerHTTPTLS",
        "TestSpanCollectorHTTPS",
        "TestSpanCollectorZipkinTLS",
        "TestGetTraceSuccessGRPC",
        "TestProxyClientTLS",
        "TestAdminServerTLS",
        "TestCreateTLSHttpServerError",
        "TestGRPCGateway_TLS_with_base_path",
        "TestGetRoundTripperTLSConfig",
        "TestReloadKeyPair",
        "TestExecuteAction",
    )

    def parse_log(self, log: str) -> TestResult:
        # Strip ANSI escape codes
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        run_tests = set(re.findall(r"=== RUN\s+([\S]+)", log))
        passed_tests = set(re.findall(r"--- PASS: ([\S]+)", log))
        skipped_individual = set(re.findall(r"--- SKIP: ([\S]+)", log))
        skipped_packages = set(re.findall(r"\?   \t([^\t]+?)\[no test files\]", log))

        def _exclude_flaky(tests: set) -> set:
            return {t for t in tests if not any(t.startswith(p) or t == p for p in self.FLAKY_TEST_PREFIXES)}

        run_tests = _exclude_flaky(run_tests)
        passed_tests = _exclude_flaky(passed_tests)
        skipped_individual = _exclude_flaky(skipped_individual)

        failed_tests = run_tests - passed_tests - skipped_individual
        skipped_tests = skipped_packages | skipped_individual
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
