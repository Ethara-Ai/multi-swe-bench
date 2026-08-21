import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class RemoteClimateDataImageBase(Image):
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
        return "continuumio/miniconda3:24.9.2-0"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            fetch = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN conda config --add channels conda-forge \\
    && conda install -y -q -n base -c conda-forge mamba \\
    && conda clean -y --all

{self.clear_env}

{fetch}
"""


class RemoteClimateDataImageDefault(Image):
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
        return RemoteClimateDataImageBase(self.pr, self.config)

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
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "install.sh",
                """#!/bin/bash
set -o pipefail
. /opt/conda/etc/profile.d/conda.sh
conda activate remote_climate_data-dev

cd /home/{pr.repo}
python - <<'PYEOF' > /tmp/rcd-pip-requirements.txt
import re

ERA_PINS = {{
    "intake/filesystem_spec": "27b3b0c4efc41ab3b6f35fc1f3e765ae034f07ff",
    "edjdavid/intake-excel": "8f0bfbce8ac5bf7a49af2a80a30476790a96be85",
    "intake/intake-xarray": "0772a2b548947cb94d32d3b89f3cb6c2fdb7dd61",
    "NCAR/intake-thredds": "a6eeeed19d34ab50117ef2eb7b2894c29eb51c23",
    "geopandas/geopandas": "1ef924270e950c6e8862335c42ace8d90e5cb3db",
    "intake/intake_geopandas": "e08c89bdd95216e9ff3f5bb6f8547799e7e7a463",
}}

requirements, in_pip = [], False
for line in open("environment.yml"):
    stripped = line.strip()
    if stripped.startswith("- pip:"):
        in_pip = True
        continue
    if not in_pip:
        continue
    match = re.match(r"^-\\s+(\\S+)", stripped)
    if not match:
        continue
    requirement = match.group(1)
    if requirement.startswith("git+"):
        slug = re.sub(r"^git\\+https://github\\.com/", "", requirement)
        slug = re.sub(r"\\.git(@.*)?$", "", slug)
        already_pinned = "@" in requirement.split("github.com/", 1)[-1]
        if not already_pinned and slug in ERA_PINS:
            requirement = "{{}}@{{}}".format(requirement, ERA_PINS[slug])
    requirements.append(requirement)
print("\\n".join(requirements))
PYEOF

echo "--- pip requirements resolved from environment.yml ---"
cat /tmp/rcd-pip-requirements.txt

while read -r requirement; do
  [ -n "$requirement" ] || continue
  pip install --no-build-isolation --no-deps --force-reinstall "$requirement" || true
done < /tmp/rcd-pip-requirements.txt

pip install --no-deps "intake==0.6.0" || true
pip install --no-deps "more-itertools==8.12.0" || true

pip install --no-deps "siphon==0.9" || true


""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
. /opt/conda/etc/profile.d/conda.sh

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

python - <<'PYEOF' > /tmp/rcd-env-conda.yml
in_pip = False
for line in open("environment.yml"):
    stripped = line.strip()
    if stripped.startswith("- pip:"):
        in_pip = True
        continue
    if in_pip:
        if stripped.startswith("- ") and (len(line) - len(line.lstrip())) <= 2:
            in_pip = False
        else:
            continue
    print(line.rstrip("\\n"))
PYEOF

mamba env create -f /tmp/rcd-env-conda.yml || true
bash /home/install.sh || true

conda activate remote_climate_data-dev
python -c "import intake, intake_xarray, intake_thredds, intake_excel, intake_geopandas, xarray, dask"
python -c "from intake_thredds.source import THREDDSMergedSource"
python -m pytest --collect-only -q -p no:sugar -p no:cacheprovider tests/test_remote_catalog.py > /tmp/rcd-collect.txt
grep -q "::" /tmp/rcd-collect.txt

mkdir -p /root/.config/fsspec /opt/fsspec-cache
cat > /root/.config/fsspec/conf.json <<'JSONEOF'
{{"simplecache": {{"cache_storage": "/opt/fsspec-cache", "same_names": true}}}}
JSONEOF

python - <<'PYEOF'
import os, sys, urllib.request

BASE = "https://psl.noaa.gov/thredds/fileServer/Datasets/ncep.reanalysis.derived"
CACHE = "/opt/fsspec-cache"
TARGETS = [
    (BASE + "/surface/air.mon.mean.nc", "air.mon.mean.nc"),
    (BASE + "/surface_gauss/air.2m.mon.mean.nc", "air.2m.mon.mean.nc"),
]

def content_length(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=120) as r:
        return int(r.headers["Content-Length"])

failed = []
for url, name in TARGETS:
    path = os.path.join(CACHE, name)
    total = content_length(url)
    for _ in range(40):
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have >= total:
            break
        req = urllib.request.Request(url, headers={{"Range": "bytes=%d-" % have}})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                with open(path, "ab") as fh:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        fh.write(chunk)
        except Exception as exc:
            print("  resume pass failed (%s), retrying" % type(exc).__name__)
    have = os.path.getsize(path) if os.path.exists(path) else 0
    print("%s: %d / %d %s" % (name, have, total, "OK" if have == total else "INCOMPLETE"))
    if have != total:
        failed.append(name)

if failed:
    sys.exit("fsspec warm-cache incomplete for: %s" % ", ".join(failed))
PYEOF

python - <<'PYEOF'
import xarray as xr
for name in ("air.mon.mean.nc", "air.2m.mon.mean.nc"):
    ds = xr.open_dataset("/opt/fsspec-cache/" + name)
    print("%s opens OK: %s" % (name, dict(ds.dims)))
PYEOF

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate remote_climate_data-dev

cd /home/{pr.repo}
bash /home/install.sh || true
python -m pytest -v -rA -p no:sugar --continue-on-collection-errors -p no:cacheprovider tests/test_remote_catalog.py

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate remote_climate_data-dev

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/install.sh || true
python -m pytest -v -rA -p no:sugar --continue-on-collection-errors -p no:cacheprovider tests/test_remote_catalog.py

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate remote_climate_data-dev

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/install.sh || true
python -m pytest -v -rA -p no:sugar --continue-on-collection-errors -p no:cacheprovider tests/test_remote_catalog.py

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        rendered = f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}
{prepare_commands}
{self.clear_env}"""

        return rendered.rstrip() + "\n"


@Instance.register("aaronspring", "remote_climate_data")
class RemoteClimateData(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RemoteClimateDataImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        progress_pattern = re.compile(
            r"^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\(.*\))?\s+\[\s*\d+%\s*\]\s*$",
            re.MULTILINE,
        )

        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"([\w./\\-]+\.py(?:::\S+)?)\s*(?:-.*)?$",
            re.MULTILINE,
        )

        for name, status in progress_pattern.findall(clean_log):
            name = name.strip()
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for status, name in summary_pattern.findall(clean_log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
