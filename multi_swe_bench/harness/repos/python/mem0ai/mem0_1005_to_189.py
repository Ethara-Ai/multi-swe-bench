import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.python.mem0ai.mem0 import (
    ImageBase,
    pr_dockerfile,
    register_era,
)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

register_era("mem0_1005_to_189", min_version=(0, 0, 0), min_anchor=0)


_REFETCH = """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
git reset --hard
###ACTION_DELIMITER###
git clean -fd
###ACTION_DELIMITER###
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
###ACTION_DELIMITER###
git checkout {pr.base.sha}
###ACTION_DELIMITER###
test -z "$(git status --porcelain)"
###ACTION_DELIMITER###
"""

_UPGRADE = """pip install --upgrade pip setuptools wheel
###ACTION_DELIMITER###
"""

_INSTALL_CALL = """bash /home/install-deps.sh
###ACTION_DELIMITER###
echo 'PYTHONPATH=/home/mem0 pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o addopts=' > test_commands.sh
###ACTION_DELIMITER###
bash test_commands.sh || true"""

_INSTALL_HEADER = """#!/bin/bash
set -e
cd /home/{pr.repo}
###ACTION_DELIMITER###
"""

_PREPARE = r"""python3 <<'PYEOF'
import tomllib, subprocess, re
poe = tomllib.load(open('pyproject.toml','rb')).get('tool',{}).get('poetry',{})
deps = poe.get('dependencies',{})
HEAVY = {'torch','torchvision','unstructured','sentence-transformers','gpt4all',
         'pymilvus','google-cloud-aiplatform','detectron2','llama-hub','replicate',
         'cohere','weaviate-client','pinecone-client','qdrant-client','opensearch-py',
         'elasticsearch','huggingface_hub'}
def conv(name, spec):
    v = spec.get('version') if isinstance(spec, dict) else spec
    if not v: return name
    v = v.strip()
    if v.startswith('^') or v.startswith('~'):
        return f'{name}=={v[1:].split(chr(44))[0].strip()}'
    if v[0].isdigit(): return f'{name}=={v}'
    return f'{name}{v}'
pk = []
for n, sp in deps.items():
    if n.lower() == 'python' or n.lower() in HEAVY: continue
    if isinstance(sp, dict) and sp.get('optional'): continue
    pk.append(conv(n, sp))
print('INSTALLING:', pk, flush=True)
subprocess.check_call(['pip','install',*pk])
PYEOF
###ACTION_DELIMITER###
python -c "import os,sysconfig; nl=chr(10); d=os.path.join(sysconfig.get_paths()['purelib'],'embedchain-0.0.0.dist-info'); os.makedirs(d,exist_ok=True); open(os.path.join(d,'METADATA'),'w').write('Metadata-Version: 2.1'+nl+'Name: embedchain'+nl+'Version: 0.0.0'+nl)"
###ACTION_DELIMITER###
pip install onnxruntime pyyaml "pytest==7.3.1" "pytest-mock==3.10.0" "pytest-env==0.8.1"
###ACTION_DELIMITER###
pip install "elasticsearch>=8.9,<9"
"""


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v --no-header -rA output (mem0 / embedchain test layout)."""
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    pattern_status_after = re.compile(
        r"^((?:tests|embedchain/tests)/.*)::(.*) (PASSED|FAILED|ERROR|SKIPPED|XFAIL)"
    )
    pattern_failed = re.compile(
        r"^FAILED ((?:tests|embedchain/tests)/.*?)::(.*?)(?= - |$)"
    )

    for line in log.splitlines():
        line = ANSI_ESCAPE.sub("", line).strip()
        match_status_after = pattern_status_after.match(line)
        if match_status_after:
            test_path = match_status_after.group(1)
            test_name = match_status_after.group(2)
            status = match_status_after.group(3)
            full_test_name = f"{test_path}::{test_name}"
            if status == "PASSED":
                passed_tests.add(full_test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(full_test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(full_test_name)
            continue
        match_failed = pattern_failed.match(line)
        if match_failed:
            test_path = match_failed.group(1)
            test_name = match_failed.group(2)
            full_test_name = f"{test_path}::{test_name}"
            failed_tests.add(full_test_name)

    passed_tests -= failed_tests
    passed_tests -= skipped_tests
    skipped_tests -= failed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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
                _REFETCH.format(pr=self.pr) + _UPGRADE + _INSTALL_CALL,
            ),
            File(
                ".",
                "install-deps.sh",
                _INSTALL_HEADER.format(pr=self.pr) + _PREPARE,
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export PYTHONPATH=/home/{pr.repo}
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export PYTHONPATH=/home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export PYTHONPATH=/home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
export http_proxy=http://127.0.0.1:9
export https_proxy=http://127.0.0.1:9
export no_proxy=
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register("mem0ai", "mem0_1005_to_189")
class MEM0_EMBEDCHAIN(Instance):
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
        return parse_pytest_log(log)
