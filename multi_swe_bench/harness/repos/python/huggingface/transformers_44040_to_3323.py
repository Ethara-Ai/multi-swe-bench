import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestResult


def _strip_binary_diffs(patch: str) -> str:
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    return "".join(s for s in sections if s and "Binary files " not in s)


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
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        fix_patch = _strip_binary_diffs(self.pr.fix_patch)
        test_patch = _strip_binary_diffs(self.pr.test_patch)
        return [
            File(
                ".",
                "fix.patch",
                fix_patch,
            ),
            File(
                ".",
                "test.patch",
                test_patch,
            ),
            File(
                ".",
                "prepare.sh",
                """ls
###ACTION_DELIMITER###
ls tests
###ACTION_DELIMITER###
pip install -e ".[testing]"
###ACTION_DELIMITER###
pip install -e ".[dev]"
###ACTION_DELIMITER###
pip install -e .
###ACTION_DELIMITER###
echo 'python -m pytest -v --no-header -rA --tb=short tests/' > test_commands.sh
###ACTION_DELIMITER###
cat test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{repo_name}
TEST_FILES=$(grep -oP '(?<=diff --git a/)\\S+(?:test_\\S+\\.py|\\S+_test\\.py)' /home/test.patch 2>/dev/null | sort -u | tr '\\n' ' ')
EXISTING_FILES=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done
if [ -n "$EXISTING_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short --continue-on-collection-errors $EXISTING_FILES 2>&1
else
    echo "No pre-existing test files found in test patch - all tests are new"
fi
""".format(
                    repo_name=repo_name
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    git -C /home/{repo_name} apply --whitespace=nowarn --3way /home/test.patch 2>/dev/null || \
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
pip install --no-deps --no-cache-dir -e . 2>/dev/null || true
TEST_FILES=$(grep -oP '(?<=diff --git a/)\\S+(?:test_\\S+\\.py|\\S+_test\\.py)' /home/test.patch 2>/dev/null | sort -u)
EXISTING_FILES=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done
if [ -n "$EXISTING_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short --continue-on-collection-errors $EXISTING_FILES 2>&1
else
    python -m pytest -v --no-header -rA --tb=short --continue-on-collection-errors tests/ 2>&1
fi
""".format(
                    repo_name=repo_name
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo_name}
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    git -C /home/{repo_name} apply --whitespace=nowarn --3way /home/test.patch 2>/dev/null || \
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
    git -C /home/{repo_name} apply --whitespace=nowarn --3way /home/fix.patch 2>/dev/null || \
    git -C /home/{repo_name} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
pip install --no-deps --no-cache-dir -e . 2>/dev/null || true
TEST_FILES=$(grep -oP '(?<=diff --git a/)\\S+(?:test_\\S+\\.py|\\S+_test\\.py)' /home/test.patch 2>/dev/null | sort -u)
EXISTING_FILES=""
for f in $TEST_FILES; do
    if [ -f "$f" ]; then
        EXISTING_FILES="$EXISTING_FILES $f"
    fi
done
if [ -n "$EXISTING_FILES" ]; then
    python -m pytest -v --no-header -rA --tb=short --continue-on-collection-errors $EXISTING_FILES 2>&1
else
    python -m pytest -v --no-header -rA --tb=short --continue-on-collection-errors tests/ 2>&1
fi
""".format(
                    repo_name=repo_name
                ),
            ),
            File(
                ".",
                "install_deps.py",
                'import re, subprocess, pathlib\n'
                'content = ""\n'
                'for f in ["setup.py", "setup.cfg", "pyproject.toml"]:\n'
                '    p = pathlib.Path(f)\n'
                '    if p.exists(): content += p.read_text()\n'
                "deps = re.findall(r'[\"\\x27]([a-zA-Z][a-zA-Z0-9_.-]*(?:[><=!~]+[^\"\\x27,\\]\\)]+)?)[\"\\x27]', content)\n"
                'known = {"accelerate","datasets","tokenizers","numpy","packaging","filelock","requests",'
                '"tqdm","regex","sacremoses","pyyaml","PyYAML","importlib_metadata","importlib-metadata",'
                '"sentencepiece","safetensors","boto3","protobuf","scipy","scikit-learn","Pillow",'
                '"huggingface-hub","huggingface_hub"}\n'
                'norm = lambda d: d.split(">")[0].split("<")[0].split("=")[0].split("!")[0].split("~")[0].strip().lower().replace("-","_")\n'
                'known_n = {k.lower().replace("-","_") for k in known}\n'
                'seen = set()\n'
                'for d in deps:\n'
                '    n = norm(d)\n'
                '    if n in known_n and n not in seen:\n'
                '        seen.add(n)\n'
                '        print(f"Installing: {d}")\n'
                '        subprocess.run(["pip", "install", "--no-cache-dir", d], check=False)\n',
            ),
            File(
                ".",
                "hub_compat.py",
                'import huggingface_hub\n'
                'import os\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfFolder"):\n'
                '    class _HfFolder:\n'
                '        @staticmethod\n'
                '        def get_token():\n'
                '            return os.environ.get("HF_TOKEN", None)\n'
                '        @staticmethod\n'
                '        def save_token(token):\n'
                '            pass\n'
                '    huggingface_hub.HfFolder = _HfFolder\n'
                '\n'
                'if not hasattr(huggingface_hub, "Repository"):\n'
                '    huggingface_hub.Repository = type("Repository", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if not hasattr(huggingface_hub, "set_access_token"):\n'
                '    huggingface_hub.set_access_token = lambda *a, **kw: None\n'
                '\n'
                'if not hasattr(huggingface_hub, "delete_repo"):\n'
                '    huggingface_hub.delete_repo = lambda *a, **kw: None\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfFileSystem"):\n'
                '    huggingface_hub.HfFileSystem = type("HfFileSystem", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if not hasattr(huggingface_hub, "HfApi"):\n'
                '    huggingface_hub.HfApi = type("HfApi", (), {"__init__": lambda self, *a, **kw: None})\n'
                '\n'
                'if hasattr(huggingface_hub, "constants"):\n'
                '    if not hasattr(huggingface_hub.constants, "HF_HUB_CACHE"):\n'
                '        huggingface_hub.constants.HF_HUB_CACHE = os.path.expanduser("~/.cache/huggingface/hub")\n'
                '\n'
                'try:\n'
                '    from huggingface_hub import utils as _hub_utils\n'
                '    if not hasattr(_hub_utils, "OfflineModeIsEnabled"):\n'
                '        class _OfflineModeIsEnabled(ConnectionError):\n'
                '            pass\n'
                '        _hub_utils.OfflineModeIsEnabled = _OfflineModeIsEnabled\n'
                'except Exception:\n'
                '    pass\n'
                '\n'
                'try:\n'
                '    import pydantic\n'
                '    if not hasattr(pydantic, "TypeAdapter"):\n'
                '        pydantic.TypeAdapter = type("TypeAdapter", (), {"__init__": lambda self, *a, **kw: None, "validate_python": lambda self, *a, **kw: None})\n'
                'except Exception:\n'
                '    pass\n'
                '\n'
                'try:\n'
                '    import builtins\n'
                '    if not hasattr(builtins, "TypeAdapter"):\n'
                '        try:\n'
                '            from pydantic import TypeAdapter\n'
                '            builtins.TypeAdapter = TypeAdapter\n'
                '        except Exception:\n'
                '            builtins.TypeAdapter = type("TypeAdapter", (), {"__init__": lambda self, *a, **kw: None})\n'
                'except Exception:\n'
                '    pass\n',
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        pr_num = self.pr.number

        if pr_num <= 25000:
            torch_pin = "'torch<2.4'"
            save_state_patch = (
                "RUN if grep -rq 'SAVE_STATE_WARNING' src/transformers/ 2>/dev/null; then \\\n"
                "      find src/transformers/ -name '*.py' -exec sed -i \\\n"
                "        's/from torch.optim.lr_scheduler import SAVE_STATE_WARNING/SAVE_STATE_WARNING = \"\"/' {} + ; \\\n"
                "    fi"
            )
        else:
            torch_pin = "'torch'"
            save_state_patch = ""

        pytest_pin = "'pytest<8.0'"

        return """
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    curl \\
    build-essential \\
    libssl-dev \\
    libffi-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \\
    if command -v apk >/dev/null 2>&1; then \\
        apk add --no-cache bash; \\
    elif command -v apt-get >/dev/null 2>&1; then \\
        apt-get update && apt-get install -y bash; \\
    fi \\
fi

WORKDIR /home/
{copy_commands}
RUN git clone https://github.com/huggingface/transformers.git /home/transformers

WORKDIR /home/transformers

RUN git reset --hard
RUN git checkout {base_sha}

RUN pip install --no-deps --no-cache-dir -e . 2>/dev/null || true

RUN cd /home/transformers && python /home/install_deps.py 2>/dev/null || true

RUN pip install --no-cache-dir {pytest_pin} pytest-xdist timeout-decorator psutil parameterized 2>/dev/null || true

RUN pip install --no-cache-dir 'huggingface-hub' 2>/dev/null || true
RUN pip install --no-cache-dir boto3 sentencepiece importlib_metadata sacremoses tokenizers accelerate torchvision 2>/dev/null || true
RUN pip install --no-cache-dir jax jaxlib fire pydantic nltk timm pytorch_lightning onnxruntime 'pytest-asyncio<0.22' openai 2>/dev/null || true
RUN pip install --no-deps --no-cache-dir datasets 2>/dev/null || true
RUN pip install --no-cache-dir --no-deps evaluate 2>/dev/null; pip install --no-cache-dir scikit-learn librosa phonemizer 2>/dev/null || true
RUN cp /home/hub_compat.py $(python -c "import site; print(site.getsitepackages()[0])")/sitecustomize.py 2>/dev/null || true
RUN find src/ tests/ -name '*.py' -exec sed -i 's/from collections import Sequence/from collections.abc import Sequence/g; s/from collections import Mapping/from collections.abc import Mapping/g; s/from collections import MutableMapping/from collections.abc import MutableMapping/g' {{}} + 2>/dev/null || true
RUN if [ -f src/transformers/dependency_versions_check.py ]; then \\
      python -c "\\
import pathlib; p=pathlib.Path('src/transformers/dependency_versions_check.py'); \\
t=p.read_text(); p.write_text(t.replace('require_version_core(deps[pkg])', 'pass  # require_version_core(deps[pkg])'))\\
"; fi

RUN pip install --no-cache-dir 'safetensors' 2>/dev/null || true
RUN pip install --no-cache-dir 'tensorflow-cpu' 2>/dev/null || \\
    pip install --no-cache-dir 'tensorflow' 2>/dev/null || true
RUN pip install --no-cache-dir 'tf-keras' 2>/dev/null || true
RUN pip install --no-cache-dir {torch_pin} --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || \\
    pip install --no-cache-dir {torch_pin} 2>/dev/null || \\
    true
{save_state_patch}

RUN pip install --no-cache-dir {pytest_pin} 2>/dev/null || true

CMD ["/bin/bash"]
""".format(
            copy_commands=copy_commands,
            base_sha=self.pr.base.sha,
            pytest_pin=pytest_pin,
            torch_pin=torch_pin,
            save_state_patch=save_state_patch,
        )


@Instance.register("huggingface", "transformers")
class HuggingFaceTransformers(Instance):
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

        # PASSED tests: "PASSED tests/test_trainer.py::Class::test_method[param]"
        passed_pattern = re.compile(r"PASSED\s+(\S+)")
        passed_tests.update(passed_pattern.findall(log))

        # XFAIL treated as passed (expected failure that actually xfailed)
        xfail_pattern = re.compile(r"XFAIL\s+(\S+)")
        passed_tests.update(xfail_pattern.findall(log))

        # FAILED tests from summary line
        failed_pattern = re.compile(r"FAILED\s+([\w/.\-]+\.py::[^\s]+)")
        failed_tests.update(failed_pattern.findall(log))

        # ERROR tests from summary line
        error_pattern = re.compile(r"ERROR\s+([\w/.\-]+\.py::[^\s]+)")
        failed_tests.update(error_pattern.findall(log))

        # SKIPPED tests from -rA short test summary
        skipped_pattern = re.compile(r"SKIPPED\s+(\S+)")
        skipped_tests.update(skipped_pattern.findall(log))

        # Ensure strict disjointness: passed takes priority over failed, failed over skipped
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
