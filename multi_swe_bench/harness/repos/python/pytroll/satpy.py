import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PYTEST = (
    "pytest -v --no-header -rA --tb=no -p no:cacheprovider "
    "satpy/tests/writer_tests"
)


class SatpyImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.8-slim-bullseye"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SatpyImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return SatpyImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

apt-get update && apt-get install -y --no-install-recommends \\
    libtiff-dev libproj-dev proj-bin proj-data \\
    libhdf5-dev libnetcdf-dev libgdal-dev gdal-bin \\
    && ln -sf /usr/include/*-linux-gnu/tiff*.h /usr/local/include/ \\
    && rm -rf /var/lib/apt/lists/*

printf '%s\\n' 'numpy==1.19.5' 'Cython<3' > /home/constraints.txt
export PIP_CONSTRAINT=/home/constraints.txt
export DISABLE_NUMCODECS_SSE2=1
export DISABLE_NUMCODECS_AVX2=1

cat > /usr/local/bin/cc-nosimd <<'CCEOF'
#!/bin/sh
args=""
for a in "$@"; do
  case "$a" in
    -msse*|-mavx*|-mno-sse*|-mno-avx*) ;;
    *) args="$args $a" ;;
  esac
done
exec /usr/bin/gcc $args
CCEOF
chmod +x /usr/local/bin/cc-nosimd
export CC=/usr/local/bin/cc-nosimd

pip install --no-cache-dir \\
    'setuptools_scm==3.5.0' \\
    'setuptools-scm-git-archive==1.1' \\
    'numpy==1.19.5' || true

pip install --no-cache-dir --no-binary pykdtree \\
    'scipy==1.5.4' \\
    'pandas==1.1.5' \\
    'xarray==0.15.1' \\
    'dask[array]==2.30.0' \\
    'pyproj==2.6.1.post1' \\
    'pykdtree==1.3.4' \\
    'pyresample==1.16.0' \\
    'configobj==5.0.6' \\
    'trollimage==1.11.0' \\
    'trollsift==0.3.4' \\
    'PyYAML==5.4.1' \\
    'zarr==2.4.0' \\
    'Pillow==7.2.0' \\
    'netCDF4==1.5.3' \\
    'h5py==2.10.0' \\
    'h5netcdf==0.8.1' \\
    'rasterio==1.1.5' \\
    'imageio==2.8.0' \\
    'mock==4.0.2' \\
    'pytest==6.2.5' || true

pip install --no-cache-dir 'libtiff==0.4.2' || true

SETUPTOOLS_SCM_PRETEND_VERSION=0.19.1 pip install --no-cache-dir --no-deps -e . || true

python -c "import satpy, pyproj, pyresample; print(satpy.__version__, pyproj.__version__, pyresample.__version__)"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{pytest}

""".format(pr=self.pr, pytest=_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{pytest}

""".format(pr=self.pr, pytest=_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{pytest}

""".format(pr=self.pr, pytest=_PYTEST),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("pytroll", "satpy")
class Satpy(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SatpyImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        pattern1 = re.compile(r"(satpy/tests/[^ \t]+)\s+(PASSED|FAILED|SKIPPED|ERROR)")
        pattern2 = re.compile(r"\b(PASSED|FAILED|SKIPPED|ERROR)\s+(satpy/tests/[^ \t]+)")
        for line in log.splitlines():
            m = pattern1.search(line)
            if m:
                test_name, status = m.group(1).strip(), m.group(2)
            else:
                m = pattern2.search(line)
                if not m:
                    continue
                status, test_name = m.group(1), m.group(2).strip()

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

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
