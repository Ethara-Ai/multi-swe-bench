import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


ELAND_380_TO_284_PR_TESTS = {
    284: [
        "tests/series/test_arithmetics_pytest.py",
    ],
    322: [
        "tests/dataframe/test_groupby_pytest.py",
    ],
    323: [
        "tests/dataframe/test_groupby_pytest.py",
        "tests/dataframe/test_metrics_pytest.py",
        "tests/series/test_metrics_pytest.py",
    ],
    355: [
        "tests/dataframe/test_describe_pytest.py",
    ],
    380: [
        "tests/dataframe/test_iterrows_itertuples_pytest.py",
    ],
}


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
        return "python:3.9-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        base_sha = self.pr.base.sha
        test_files = ELAND_380_TO_284_PR_TESTS.get(self.pr.number, [])
        test_files_str = " ".join(test_files)

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
                f"""#!/bin/bash
set -e
cd /home/{repo_name}

# 1. Checkout base commit
git checkout {base_sha}

# 2. Install from package files (setup.py + requirements-dev.txt)
pip install --upgrade pip
pip install "setuptools<70"
pip install -e .
pip install -r requirements-dev.txt

# 3. Pin overrides for known compat issues (Docker-verified)
# pandas>=1.3 removes ABCIndexClass used by PRs 322/323/355
pip install "pandas>=1.2,<1.3" "numpy<1.24" "elasticsearch>=7.7,<8"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

sudo -u elasticsearch /usr/share/elasticsearch/bin/elasticsearch -d
until curl -s "localhost:9200/_cluster/health?wait_for_status=yellow&timeout=60s" > /dev/null 2>&1; do sleep 2; done
python -m tests.setup_tests

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

sudo -u elasticsearch /usr/share/elasticsearch/bin/elasticsearch -d
until curl -s "localhost:9200/_cluster/health?wait_for_status=yellow&timeout=60s" > /dev/null 2>&1; do sleep 2; done
python -m tests.setup_tests

if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

sudo -u elasticsearch /usr/share/elasticsearch/bin/elasticsearch -d
until curl -s "localhost:9200/_cluster/health?wait_for_status=yellow&timeout=60s" > /dev/null 2>&1; do sleep 2; done
python -m tests.setup_tests

if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    echo "No test files found"
    exit 1
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.9-bookworm

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git sudo curl gnupg2 openjdk-17-jre-headless && \\
    curl -fsSL https://artifacts.elastic.co/GPG-KEY-elasticsearch | apt-key add - && \\
    echo "deb https://artifacts.elastic.co/packages/7.x/apt stable main" > /etc/apt/sources.list.d/elastic-7.x.list && \\
    apt-get update && apt-get install -y elasticsearch && \\
    echo "discovery.type: single-node" >> /etc/elasticsearch/elasticsearch.yml && \\
    echo "xpack.security.enabled: false" >> /etc/elasticsearch/elasticsearch.yml && \\
    mkdir -p /var/run/elasticsearch && chown elasticsearch:elasticsearch /var/run/elasticsearch && \\
    rm -rf /var/lib/apt/lists/*

ENV ELASTICSEARCH_HOST=localhost
ENV TEST_SUITE=free

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("elastic", "eland_380_to_284")
class ELAND_380_TO_284(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in log.split("\n"):
            line = line.strip()
            if line.startswith("PASSED "):
                test_name = line[len("PASSED "):].strip()
                passed_tests.add(test_name)
            elif line.startswith("FAILED "):
                test_name = line[len("FAILED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                failed_tests.add(test_name)
            elif line.startswith("SKIPPED "):
                test_name = line[len("SKIPPED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                skipped_tests.add(test_name)
            else:
                match = re.match(
                    r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\s*(\[.*\])?$", line
                )
                if match:
                    test_name = match.group(1)
                    status = match.group(2)
                    if status == "PASSED":
                        passed_tests.add(test_name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(test_name)
                    elif status == "SKIPPED":
                        skipped_tests.add(test_name)
                    elif status == "XFAIL":
                        passed_tests.add(test_name)

        # Conflict resolution: if test in both passed and failed, keep failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
