import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# aaronspring/remote_climate_data#48 "NCEP reanalysis" adds NCEP catalog entries to
# catalogs/atmosphere.yaml + an assertion in tests/test_remote_catalog.py that the NCEP
# THREDDS source loads (`item(year="19?0").to_dask()`). The graded transition is the two
# THREDDS NCEP items (NCEP_6h, NCEP_6h_gauss) that the PR's assertion covers; they fetch live
# data from psl.noaa.gov THREDDS and pass. We scope pytest to `-k NCEP_6h` so the run doesn't
# depend on the ~39 other catalog items (many pointing at dead remote servers), and pin the
# intake stack to the only mutually-compatible set (validated empirically):
#   intake==0.6.0        (0.6.2+ auto-discovers entry_points -> "Driver already enabled")
#   intake-thredds==2021.6.16  (needs intake>=0.6; 2022+ needs intake>2)
#   intake-xarray==0.5.0 (0.6+ does `from intake import readers`, an intake-2 API)
# The whole viz stack in environment.yml (cartopy/hvplot/nodejs/jupyterlab) is dropped — the
# test never imports it. python=3.6 (env default) is bumped to 3.9 for conda-forge availability;
# intake 0.6.0's walk/catalog API used by the test is unchanged.

_ENV = "rcd"
_ENV_CREATE = (
    'mamba create -y -n rcd python=3.9 '
    '"intake=0.6.0" "intake-thredds=2021.6.16" "intake-xarray=0.5.0" intake-geopandas '
    'xarray dask pydap netcdf4 h5netcdf siphon fsspec aiohttp requests cftime '
    'geopandas pytest pandas "xlrd=1.2.0"'
)
_PIP_EXTRA = 'pip install --no-deps "git+https://github.com/edjdavid/intake-excel.git"'
# only the two THREDDS NCEP items the PR asserts (NCEP_6h, NCEP_6h_gauss)
_TEST_CMD = (
    "python -m pytest -k 'NCEP_6h' -v -rA -p no:sugar "
    "-p no:cacheprovider tests/test_remote_catalog.py"
)


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

    def image_prefix(self) -> str:
        return "envagent"

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
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN conda config --add channels conda-forge \\
    && conda install -y -q -n base -c conda-forge mamba \\
    && conda clean -y --all

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}

# validated minimal env (see module docstring for the pin rationale)
RUN {_ENV_CREATE}
RUN . /opt/conda/etc/profile.d/conda.sh && conda activate {_ENV} && {_PIP_EXTRA}
RUN . /opt/conda/etc/profile.d/conda.sh && conda activate {_ENV} \\
    && python -c "import intake, intake_xarray, intake_thredds, intake_excel, intake_geopandas, xarray, dask; print('imports ok', intake.__version__)"

{self.clear_env}

CMD ["/bin/bash"]
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
        return RemoteClimateDataImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard >/dev/null 2>&1 || true
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate {env}
cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, env=_ENV, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate {env}
cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(pr=self.pr, env=_ENV, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
. /opt/conda/etc/profile.d/conda.sh
conda activate {env}
cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(pr=self.pr, env=_ENV, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}
"""


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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        # pytest -v progress lines:  path::test[param] PASSED [ xx%]
        prog = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b", re.MULTILINE
        )
        # pytest -rA summary lines:  PASSED path::test[param]
        summ = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)", re.MULTILINE
        )
        for name, status in prog.findall(clean):
            (passed if status in ("PASSED", "XPASS") else failed if status in ("FAILED", "ERROR") else skipped).add(name)
        for status, name in summ.findall(clean):
            (passed if status in ("PASSED", "XPASS") else failed if status in ("FAILED", "ERROR") else skipped).add(name)
        passed -= failed
        passed -= skipped
        skipped -= failed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
