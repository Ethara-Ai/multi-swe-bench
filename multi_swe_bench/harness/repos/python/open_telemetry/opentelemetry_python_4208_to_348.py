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
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                """ls -la
###ACTION_DELIMITER###
apt-get update
###ACTION_DELIMITER###
apt-get install -y build-essential
###ACTION_DELIMITER###
pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
pip install -e ./opentelemetry-api
###ACTION_DELIMITER###
pip install -e ./opentelemetry-sdk
###ACTION_DELIMITER###
pip install -e ./opentelemetry-semantic-conventions
###ACTION_DELIMITER###
pip install -e ./opentelemetry-proto
###ACTION_DELIMITER###
pip install -r dev-requirements.txt
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-otlp
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-zipkin
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-jaeger
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-flask
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-requests
###ACTION_DELIMITER###
pip install -e ./ext/opentelemetry-ext-grpc
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-otlp
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-otlp-proto-grpc
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-otlp-proto-http
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-jaeger
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-jaeger-proto-grpc
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-jaeger-thrift
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-zipkin
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-zipkin-json
###ACTION_DELIMITER###
pip install -e ./exporter/opentelemetry-exporter-zipkin-proto-http
###ACTION_DELIMITER###
pip install -e ./propagator/opentelemetry-propagator-b3
###ACTION_DELIMITER###
pip install -e ./propagator/opentelemetry-propagator-jaeger
###ACTION_DELIMITER###
pip install -e ./shim/opentelemetry-opentracing-shim
###ACTION_DELIMITER###
pip install -e ./opentelemetry-distro
###ACTION_DELIMITER###
pip install -e ./instrumentation/opentelemetry-instrumentation
###ACTION_DELIMITER###
pip install -e ./tests/util
###ACTION_DELIMITER###
if [ -f scripts/eachdist.py ]; then python scripts/eachdist.py develop; fi
###ACTION_DELIMITER###
pip install tox
###ACTION_DELIMITER###
pip install protobuf
###ACTION_DELIMITER###
pip install 'protobuf<=3.20.0'
###ACTION_DELIMITER###
PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python pip install protobuf
###ACTION_DELIMITER###
if [ -f scripts/eachdist.py ]; then python scripts/eachdist.py test; fi
###ACTION_DELIMITER###
if [ -f tox.ini ]; then tox; fi
###ACTION_DELIMITER###
pytest -v --no-header -rA --tb=no -p no:cacheprovider opentelemetry-api/tests opentelemetry-sdk/tests
###ACTION_DELIMITER###
echo 'done with preparation'""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
PYTEST_ARGS="-v --no-header -rA --tb=no -p no:cacheprovider"
if [ -f scripts/eachdist.py ]; then
    python scripts/eachdist.py test -- $PYTEST_ARGS
elif [ -f tox.ini ]; then
    tox
else
    pytest $PYTEST_ARGS
fi
for d in ext/*/tests exporter/*/tests propagator/*/tests shim/*/tests instrumentation/*/tests; do
    if [ -d "$d" ]; then
        echo ">>> pytest $d $PYTEST_ARGS"
        pytest "$d" $PYTEST_ARGS 2>&1 || true
    fi
done

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
PYTEST_ARGS="-v --no-header -rA --tb=no -p no:cacheprovider"
if [ -f scripts/eachdist.py ]; then
    python scripts/eachdist.py test -- $PYTEST_ARGS
elif [ -f tox.ini ]; then
    tox
else
    pytest $PYTEST_ARGS
fi
for d in ext/*/tests exporter/*/tests propagator/*/tests shim/*/tests instrumentation/*/tests; do
    if [ -d "$d" ]; then
        echo ">>> pytest $d $PYTEST_ARGS"
        pytest "$d" $PYTEST_ARGS 2>&1 || true
    fi
done

""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
PYTEST_ARGS="-v --no-header -rA --tb=no -p no:cacheprovider"
if [ -f scripts/eachdist.py ]; then
    python scripts/eachdist.py test -- $PYTEST_ARGS
elif [ -f tox.ini ]; then
    tox
else
    pytest $PYTEST_ARGS
fi
for d in ext/*/tests exporter/*/tests propagator/*/tests shim/*/tests instrumentation/*/tests; do
    if [ -d "$d" ]; then
        echo ">>> pytest $d $PYTEST_ARGS"
        pytest "$d" $PYTEST_ARGS 2>&1 || true
    fi
done

""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
# Dockerfile for open-telemetry/opentelemetry-python PRs 348-4208

# Base image: python:3.9-slim covers the full PR range
FROM python:3.9-slim

## Set noninteractive
ENV DEBIAN_FRONTEND=noninteractive

# Install basic requirements
RUN apt-get update && apt-get install -y git

# Ensure bash is available
RUN if [ ! -f /bin/bash ]; then         if command -v apk >/dev/null 2>&1; then             apk add --no-cache bash;         elif command -v apt-get >/dev/null 2>&1; then             apt-get update && apt-get install -y bash;         elif command -v yum >/dev/null 2>&1; then             yum install -y bash;         else             exit 1;         fi     fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/open-telemetry/opentelemetry-python.git /home/opentelemetry-python

WORKDIR /home/opentelemetry-python
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel && \
    pip install tox pytest pytest-benchmark protobuf 'protobuf<=3.20.0' grpcio-tools 2>/dev/null; true

RUN pip install -e . 2>/dev/null; true
RUN pip install -e ./opentelemetry-api 2>/dev/null; true
RUN pip install -e ./opentelemetry-sdk 2>/dev/null; true
RUN pip install -e ./opentelemetry-semantic-conventions 2>/dev/null; true
RUN pip install -e ./opentelemetry-proto 2>/dev/null; true
RUN if [ -f dev-requirements.txt ]; then pip install -r dev-requirements.txt 2>/dev/null; fi; true
RUN for d in ext/*/; do [ -d "$d" ] && pip install -e "$d" 2>/dev/null; done; true
RUN for d in exporter/*/; do [ -d "$d" ] && pip install -e "$d" 2>/dev/null; done; true
RUN for d in propagator/*/; do [ -d "$d" ] && pip install -e "$d" 2>/dev/null; done; true
RUN for d in shim/*/; do [ -d "$d" ] && pip install -e "$d" 2>/dev/null; done; true
RUN for d in instrumentation/*/; do [ -d "$d" ] && pip install -e "$d" 2>/dev/null; done; true
RUN pip install -e ./opentelemetry-distro 2>/dev/null; true
RUN pip install -e ./tests/util 2>/dev/null; true
RUN if [ -f scripts/eachdist.py ]; then python scripts/eachdist.py develop 2>/dev/null; fi; true
RUN pip install "setuptools<70" 2>/dev/null; true
"""
        dockerfile_content += f"""
{copy_commands}
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("open-telemetry", "opentelemetry-python")
class OPENTELEMETRY_PYTHON_4208_TO_348(Instance):
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

        # Pattern for pytest verbose output: test_path::test_name PASSED/FAILED/SKIPPED
        # Use [ \t]+ instead of \s+ to avoid matching across newlines
        pattern = r"([\w/\-\.]+?\.py::[\w:]+)[ \t]+(PASSED|FAILED|SKIPPED)"
        matches = re.findall(pattern, log, re.IGNORECASE)
        for test_name, status in matches:
            status = status.upper()
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status == "FAILED":
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # Also capture failed test headers (e.g., ______________ TestName _______________)
        failed_header_pattern = re.compile(
            r"^\s*\[\s*\d+\]\s*_{3,}\s*([\w.]+::?[\w.]+)\s*_{3,}\s*$", re.MULTILINE
        )
        for match in failed_header_pattern.finditer(log):
            test_name = match.group(1)
            full_test_match = re.search(
                rf"[\w/\-\.]+/tests/[\w/\-\.]+\.py::{test_name}", log
            )
            if full_test_match:
                failed_tests.add(full_test_match.group(0))
            else:
                failed_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
