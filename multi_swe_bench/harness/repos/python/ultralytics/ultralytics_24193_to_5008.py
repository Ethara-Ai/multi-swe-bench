import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class UltralyticsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.11-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git libgl1 libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
"""


class UltralyticsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return UltralyticsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
git diff --name-only
git diff --name-only --cached
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

git reset --hard
git checkout --force {base_sha}
git clean -fd

# Install CPU-only torch (avoids massive CUDA download)
pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install ultralytics with dev deps (handles both setup.py and pyproject.toml eras)
pip install --no-cache-dir -e '.[dev]' || pip install --no-cache-dir -e '.'

# Install pytest if not already present (fallback for very old PRs)
pip install --no-cache-dir pytest pytest-cov 2>/dev/null || true

# Patch torch.load for old ultralytics versions (pre-8.2) that don't pass weights_only=False
# torch>=2.6 changed default to weights_only=True, breaking old code
python3 -c "
import site, os
sc_path = os.path.join(site.getsitepackages()[0], 'sitecustomize.py')
with open(sc_path, 'w') as f:
    f.write('''import torch
_orig_load = torch.load
def _patched_load(*args, **kwargs):
    if \"weights_only\" not in kwargs:
        kwargs[\"weights_only\"] = False
    return _orig_load(*args, **kwargs)
torch.load = _patched_load
''')
print(f'Created {{sc_path}}')
"

# Warm up: verify import works
python3 -c "import ultralytics; print(f'ultralytics {{ultralytics.__version__}} ready')"
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}
pytest tests/ --ignore=tests/test_cuda.py --tb=short -q 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Clean any previous patch state
git checkout -- . 2>/dev/null || true

# Apply test patch
if ! git apply --whitespace=nowarn /home/test.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
fi

# Reinstall in case test patch modified setup files
pip install --no-cache-dir -e '.[dev]' 2>/dev/null || pip install --no-cache-dir -e '.' 2>/dev/null || true

# Run tests
pytest tests/ --ignore=tests/test_cuda.py --tb=short -q 2>&1
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{repo}

# Clean any previous patch state
git checkout -- . 2>/dev/null || true

# Apply test patch first, then fix patch
if ! git apply --whitespace=nowarn /home/test.patch; then
    git apply --whitespace=nowarn --reject /home/test.patch || true
fi
if ! git apply --whitespace=nowarn /home/fix.patch; then
    git apply --whitespace=nowarn --reject /home/fix.patch || true
fi

# Reinstall in case patches modified setup files
pip install --no-cache-dir -e '.[dev]' 2>/dev/null || pip install --no-cache-dir -e '.' 2>/dev/null || true

# Run tests
pytest tests/ --ignore=tests/test_cuda.py --tb=short -q 2>&1
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("ultralytics", "ultralytics_24193_to_5008")
class Ultralytics(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UltralyticsImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_re.sub("", log)

        # tests/test_python.py::test_workflow PASSED
        verbose_re = re.compile(
            r"^(tests/\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
            re.MULTILINE,
        )
        for m in verbose_re.finditer(log):
            test_name = m.group(1)
            status = m.group(2)
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        # tests/test_python.py .F.sxX..
        compact_re = re.compile(r"^(tests/\S+\.py)\s+([.FEsxX]+)", re.MULTILINE)
        for m in compact_re.finditer(log):
            test_file = m.group(1)
            results = m.group(2)
            for i, symbol in enumerate(results):
                test_name = f"{test_file}::test_{i + 1}"
                if symbol == ".":
                    passed_tests.add(test_name)
                elif symbol in ("F", "E"):
                    failed_tests.add(test_name)
                elif symbol in ("s", "x", "X"):
                    skipped_tests.add(test_name)

        # FAILED tests/test_python.py::test_workflow - AssertionError
        failed_summary_re = re.compile(
            r"^FAILED\s+(tests/\S+::\S+)", re.MULTILINE
        )
        for m in failed_summary_re.finditer(log):
            test_name = m.group(1)
            failed_tests.add(test_name)
            passed_tests.discard(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
