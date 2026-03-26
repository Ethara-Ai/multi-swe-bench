import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "streamlit"


class ImageDefault_14289_13833(Image):
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
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
git checkout {self.pr.base.sha}

# ── Install all system deps up front ──
export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get install -y --no-install-recommends \\
    build-essential make curl git rsync unzip \\
    protobuf-compiler libprotobuf-dev \\
    libmysqlclient-dev || apt-get install -y --no-install-recommends libmariadb-dev || true

# ── Install Node.js 20 + corepack + yarn ──
if ! command -v node &>/dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi
corepack enable || true
corepack prepare yarn@4.5.3 --activate 2>/dev/null || true
npm install -g yarn 2>/dev/null || true

# ── Install uv for Python dependency management ──
pip install --no-cache-dir uv 2>/dev/null || true

# ── Install Python build deps ──
pip install --no-cache-dir \\
    'types-protobuf<6' 'mypy-protobuf<4' \\
    setuptools wheel || true

# ── Try make all / make init (builds protobuf + frontend + python deps) ──
export MYSQLCLIENT_CFLAGS="-I/usr/include/mariadb" 2>/dev/null || true
export MYSQLCLIENT_LDFLAGS="-L/usr/lib/x86_64-linux-gnu -lmariadb" 2>/dev/null || true

# ── Create venv for uv compatibility ──
python -m venv .venv 2>/dev/null || true
export PATH="/home/streamlit/.venv/bin:$PATH"
export UV_PROJECT_ENVIRONMENT="/home/streamlit/.venv"
export VIRTUAL_ENV="/home/streamlit/.venv"

# ── Patch Makefile to add -I/usr/include for protobuf well-known types ──
sed -i 's|--proto_path=proto|--proto_path=proto -I/usr/include|g' Makefile 2>/dev/null || true

make all USE_CONSTRAINTS_FILE=false 2>&1 || \\
make all 2>&1 || \\
make init USE_CONSTRAINTS_FILE=false 2>&1 || \\
make init 2>&1 || true

# ── Fallback: manual protobuf compilation if make failed ──
if ! python -c "import streamlit.proto.BackMsg_pb2" 2>/dev/null; then
    echo "=== Protobuf compilation failed via make, trying manual protoc ==="
    if [ -d "proto/streamlit/proto" ]; then
        protoc -I/usr/include --proto_path=proto --python_out=lib proto/streamlit/proto/*.proto 2>/dev/null || true
    fi
fi

# ── Fallback: install test deps directly if make didn't do it ──
if ! python -c "import pytest" 2>/dev/null; then
    echo "=== pytest not found, installing test deps manually ==="
    pip install --no-cache-dir -e "lib/[test]" 2>/dev/null || \\
    pip install --no-cache-dir -e "lib/" 2>/dev/null || \\
    pip install --no-cache-dir -r lib/test-requirements.txt 2>/dev/null || \\
    pip install --no-cache-dir -r lib/dev-requirements.txt 2>/dev/null || true
    pip install --no-cache-dir pytest pytest-xdist pytest-cov 2>/dev/null || true
fi

echo "=== prepare.sh done ==="
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export PATH="/home/streamlit/.venv/bin:$PATH"
cd /home/{REPO_DIR}
cd lib
PYTHONPATH=. pytest -v --tb=short -l --no-cov -p no:cacheprovider -o "addopts=" --deselect tests/streamlit/streamlit_test.py::StreamlitTest::test_streamlit_version --deselect tests/streamlit/external/langchain/streamlit_callback_handler_test.py::StreamlitCallbackHandlerAPITest::test_import_from_langchain --deselect tests/streamlit/elements/chat_test.py::ChatTest::test_chat_input_max_chars tests/ 2>&1 || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export PATH="/home/streamlit/.venv/bin:$PATH"
cd /home/{REPO_DIR}
git apply --whitespace=nowarn \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/test.patch 2>/dev/null || \\
git apply --whitespace=nowarn --reject \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/test.patch 2>/dev/null || true
find . -name '*.rej' -delete 2>/dev/null || true
# Recompile protobuf in case patches added new .proto files
protoc -I/usr/include --proto_path=proto --python_out=lib proto/streamlit/proto/*.proto 2>/dev/null || true
cd lib
PYTHONPATH=. pytest -v --tb=short -l --no-cov -p no:cacheprovider -o "addopts=" --deselect tests/streamlit/streamlit_test.py::StreamlitTest::test_streamlit_version --deselect tests/streamlit/external/langchain/streamlit_callback_handler_test.py::StreamlitCallbackHandlerAPITest::test_import_from_langchain --deselect tests/streamlit/elements/chat_test.py::ChatTest::test_chat_input_max_chars tests/ 2>&1 || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export PATH="/home/streamlit/.venv/bin:$PATH"
cd /home/{REPO_DIR}
# Apply test patch
git apply --whitespace=nowarn \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/test.patch 2>/dev/null || \\
git apply --whitespace=nowarn --reject \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/test.patch 2>/dev/null || true
find . -name '*.rej' -delete 2>/dev/null || true
# Apply fix patch
git apply --whitespace=nowarn \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/fix.patch 2>/dev/null || \\
git apply --whitespace=nowarn --reject \\
    --exclude='*.png' --exclude='*.jpg' --exclude='*.gif' --exclude='*.ico' \\
    --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' \\
    --exclude='*.sqlite' --exclude='*.db' \\
    /home/fix.patch 2>/dev/null || true
find . -name '*.rej' -delete 2>/dev/null || true
# Recompile protobuf in case patches added new .proto files
protoc -I/usr/include --proto_path=proto --python_out=lib proto/streamlit/proto/*.proto 2>/dev/null || true
cd lib
PYTHONPATH=. pytest -v --tb=short -l --no-cov -p no:cacheprovider -o "addopts=" --deselect tests/streamlit/streamlit_test.py::StreamlitTest::test_streamlit_version --deselect tests/streamlit/external/langchain/streamlit_callback_handler_test.py::StreamlitCallbackHandlerAPITest::test_import_from_langchain --deselect tests/streamlit/elements/chat_test.py::ChatTest::test_chat_input_max_chars tests/ 2>&1 || true
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM python:3.10-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential make curl rsync unzip \\
    protobuf-compiler libprotobuf-dev \\
    libmariadb-dev \\
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 + corepack + yarn
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \\
    && apt-get install -y --no-install-recommends nodejs \\
    && corepack enable \\
    && corepack prepare yarn@4.5.3 --activate \\
    && rm -rf /var/lib/apt/lists/*

# Pre-install Python protobuf deps + uv
RUN pip install --no-cache-dir 'types-protobuf<6' 'mypy-protobuf<4' uv pytest-cov

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{REPO_DIR}.git /home/{REPO_DIR} \\
    && (git -C /home/{REPO_DIR} fetch origin {self.pr.base.ref} || true) \\
    && git -C /home/{REPO_DIR} fetch origin {self.pr.base.sha}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("streamlit", "streamlit_14289_to_13833")
class STREAMLIT_14289_TO_13833(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_14289_13833(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Match pytest output: tests/streamlit/foo/test_bar.py::TestClass::test_name PASSED [  5%]
        pattern = re.compile(
            r"^\s*(?:\[\s*\d+\s*\]\s*)?(tests/.*?)\s+(?:\x1b\[[0-9;]*m)*\s*(PASSED|SKIPPED|FAILED)\s*(?:\x1b\[[0-9;]*m)*\s*(?:\[\s*\d+%\s*\])?"
        )
        for line in log.splitlines():
            match = pattern.match(line)
            if match:
                test_name = match.group(1).strip()
                status = match.group(2)
                if status == "PASSED":
                    passed_tests.add(test_name)
                elif status == "SKIPPED":
                    skipped_tests.add(test_name)
                elif status == "FAILED":
                    failed_tests.add(test_name)

        # Also parse FAILED summary lines
        summary_pattern = re.compile(r"^FAILED\s+(tests/.*)$", re.MULTILINE)
        for match in summary_pattern.finditer(log):
            failed_tests.add(match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
