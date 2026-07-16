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

# v0.0.24 .. v0.1.33 -- embedchain in-tree, poetry build backend.
register_era("mem0_1005_to_189", min_version=(0, 0, 0), min_anchor=0)

# embedchain is imported by the tests of this era but is not always installed as
# a distribution, so importlib.metadata lookups raise PackageNotFoundError at
# collection time. Synthesising a dist-info directory satisfies the lookup.
_EMBEDCHAIN_DISTINFO = (
    "RUN python -c \"import os,sysconfig; nl=chr(10); "
    "d=os.path.join(sysconfig.get_paths()['purelib'],'embedchain-0.0.0.dist-info'); "
    "os.makedirs(d,exist_ok=True); "
    "open(os.path.join(d,'METADATA'),'w').write("
    "'Metadata-Version: 2.1'+nl+'Name: embedchain'+nl+'Version: 0.0.0'+nl)\" || true"
)

# Runs in the per-PR layer, in WORKDIR /home/mem0 after the ${BASE_COMMIT}
# checkout and before the hardening block, so the project is installed at this
# PR's commit. It cannot live in the shared base: the base has no repo.
# poetry is pinned to 1.6.1 -- this era's lockfiles predate the poetry 2.x
# lockfile format and a newer poetry refuses to read them.
# This era has NO poetry.lock, so `poetry install` re-resolves from scratch and
# hangs (>>10 min) -- and the setuptools flat-layout build backend makes
# `pip install .`/`-e .` fail on package discovery. Both silent-|| paths left the
# image with none of embedchain's deps -> every test file ImportError'd at
# collection -> (0,0,0). Instead, read THIS commit's own non-optional deps +
# lightweight dataloaders straight from pyproject.toml and pip-install them
# (seconds, no resolve). Heavy ML extras (torch/unstructured/sentence-transformers
# /...) are skipped: not needed to import embedchain or collect its tests. WORKDIR
# is /home/mem0 here; base declares `# syntax=docker/dockerfile:1.6` so heredoc RUN
# works, and python3 is 3.11 (tomllib in stdlib).
_INSTALL = (
    "RUN pip install --upgrade pip setuptools wheel\n"
    "RUN python3 <<'PYEOF' || true\n"
    "import tomllib, subprocess, re\n"
    "poe = tomllib.load(open('pyproject.toml','rb')).get('tool',{}).get('poetry',{})\n"
    "deps, extras = poe.get('dependencies',{}), poe.get('extras',{})\n"
    "HEAVY = {'torch','torchvision','unstructured','sentence-transformers','gpt4all',\n"
    "         'pymilvus','google-cloud-aiplatform','detectron2','llama-hub','replicate',\n"
    "         'cohere','weaviate-client','pinecone-client','qdrant-client','opensearch-py',\n"
    "         'elasticsearch','huggingface_hub'}\n"
    "def conv(name, spec):\n"
    "    v = spec.get('version') if isinstance(spec, dict) else spec\n"
    "    if not v: return name\n"
    "    v = v.strip()\n"
    "    if v.startswith('^'):\n"
    "        b = v[1:].split(',')[0].strip()\n"
    "        p = [int(x) for x in re.findall(r'\\d+', b)] + [0, 0, 0]\n"
    "        hi = f'{p[0]+1}.0.0' if p[0] else (f'0.{p[1]+1}.0' if p[1] else f'0.0.{p[2]+1}')\n"
    "        return f'{name}>={b},<{hi}'\n"
    "    if v.startswith('~'):\n"
    "        b = v[1:].strip(); p = [int(x) for x in re.findall(r'\\d+', b)] + [0, 0]\n"
    "        return f'{name}>={b},<{p[0]}.{p[1]+1}.0'\n"
    "    if v[0].isdigit(): return f'{name}=={v}'\n"
    "    return f'{name}{v}'\n"
    "pk, seen = [], set()\n"
    "for n, s in deps.items():\n"
    "    if n.lower()=='python' or n.lower() in HEAVY: continue\n"
    "    if isinstance(s, dict) and s.get('optional'): continue\n"
    "    pk.append(conv(n, s)); seen.add(n.lower())\n"
    "for n in extras.get('dataloaders', []):\n"
    "    r = 'youtube-transcript-api' if n.lower().startswith('youtube') else n\n"
    "    if r.lower() in HEAVY or r.lower() in seen: continue\n"
    "    pk.append(conv(r, deps.get(n) or deps.get(r) or '')); seen.add(r.lower())\n"
    "print('INSTALLING:', pk, flush=True)\n"
    "subprocess.run(['pip','install',*pk])\n"
    "PYEOF\n"
    f"{_EMBEDCHAIN_DISTINFO}\n"
    "RUN pip install onnxruntime pyyaml pytest pytest-mock pytest-asyncio pytest-env || true"
)


def parse_pytest_log(log: str) -> TestResult:
    """Parse pytest -v --no-header -rA output (mem0 / embedchain test layout)."""
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    pattern_status_after = re.compile(
        r"^((?:tests|embedchain/tests)/.*)::(.*) (PASSED|SKIPPED|XFAIL)"
    )
    pattern_failed = re.compile(
        r"^FAILED ((?:tests|embedchain/tests)/.*)::(.*)"
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
                """pip install --upgrade pip setuptools wheel && pip install "poetry==1.6.1"
###ACTION_DELIMITER###
poetry config virtualenvs.create false 2>/dev/null || true
###ACTION_DELIMITER###
timeout 600 poetry install --no-root --no-interaction 2>/dev/null || pip install -e . 2>/dev/null || pip install . 2>/dev/null || true
###ACTION_DELIMITER###
python -c "import os,sysconfig; nl=chr(10); d=os.path.join(sysconfig.get_paths()['purelib'],'embedchain-0.0.0.dist-info'); os.makedirs(d,exist_ok=True); open(os.path.join(d,'METADATA'),'w').write('Metadata-Version: 2.1'+nl+'Name: embedchain'+nl+'Version: 0.0.0'+nl)" || true
###ACTION_DELIMITER###
pip install onnxruntime pytest pytest-mock pytest-asyncio pytest-env || true""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
export PYTHONPATH=/home/{pr.repo}
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
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
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
git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
git -C /home/{pr.repo} apply --whitespace=nowarn /home/fix.patch 2>/dev/null || git -C /home/{pr.repo} apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
pytest tests/ -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors -o 'addopts='
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self, _INSTALL)


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
