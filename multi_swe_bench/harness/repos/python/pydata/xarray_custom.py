import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Data for all 12 PRs - extracted from JSONL
PR_CONFIG = {
    "xarray_0_10_3_to_0_10_4": {
        "python_version": "3.6",
        "test_files": [
            "xarray/tests/__init__.py",
            "xarray/tests/test_accessors.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_cftimeindex.py",
            "xarray/tests/test_coding_strings.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_conventions.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_utils.py",
        ],
    },
    "xarray_0_10_6_to_0_10_7": {
        "python_version": "3.6",
        "test_files": [
            "xarray/tests/test_backends.py",
            "xarray/tests/test_distributed.py",
            "xarray/tests/test_interp.py",
            "xarray/tests/test_plot.py",
        ],
    },
    "xarray_0_12_0_to_0_12_1": {
        "python_version": "3.6",
        "test_files": [
            "xarray/testing.py",
            "xarray/tests/__init__.py",
            "xarray/tests/test_combine.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_interp.py",
        ],
    },
    "xarray_0_12_2_to_0_12_3": {
        "python_version": "3.6",
        "test_files": [
            "conftest.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_backends_common.py",
            "xarray/tests/test_backends_file_manager.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_concat.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_print_versions.py",
            "xarray/tests/test_variable.py",
        ],
    },
    "xarray_0_15_0_to_0_15_1": {
        "python_version": "3.8",
        "test_files": [
            "conftest.py",
            "xarray/tests/__init__.py",
            "xarray/tests/test_accessor_dt.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_cftimeindex.py",
            "xarray/tests/test_combine.py",
            "xarray/tests/test_concat.py",
            "xarray/tests/test_dask.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_duck_array_ops.py",
            "xarray/tests/test_formatting.py",
            "xarray/tests/test_formatting_html.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_interp.py",
            "xarray/tests/test_options.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_sparse.py",
            "xarray/tests/test_units.py",
            "xarray/tests/test_variable.py",
            "xarray/tests/test_weighted.py",
        ],
    },
    "xarray_0_16_0_to_0_16_1": {
        "python_version": "3.8",
        "test_files": [
            "conftest.py",
            "xarray/testing.py",
            "xarray/tests/__init__.py",
            "xarray/tests/test_accessor_str.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_backends_file_manager.py",
            "xarray/tests/test_cftime_offsets.py",
            "xarray/tests/test_cftimeindex.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_combine.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_concat.py",
            "xarray/tests/test_cupy.py",
            "xarray/tests/test_dask.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_duck_array_ops.py",
            "xarray/tests/test_formatting.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_indexing.py",
            "xarray/tests/test_interp.py",
            "xarray/tests/test_merge.py",
            "xarray/tests/test_missing.py",
            "xarray/tests/test_nputils.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_sparse.py",
            "xarray/tests/test_testing.py",
            "xarray/tests/test_units.py",
            "xarray/tests/test_variable.py",
            "xarray/tests/test_weighted.py",
        ],
    },
    "xarray_2022_10_0_to_2022_11_0": {
        "python_version": "3.10",
        "test_files": [
            "xarray/tests/__init__.py",
            "xarray/tests/test_array_api.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_backends_file_manager.py",
            "xarray/tests/test_coarsen.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_dask.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_duck_array_ops.py",
            "xarray/tests/test_formatting.py",
            "xarray/tests/test_formatting_html.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_missing.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_plugins.py",
            "xarray/tests/test_sparse.py",
            "xarray/tests/test_tutorial.py",
            "xarray/tests/test_units.py",
            "xarray/tests/test_utils.py",
            "xarray/tests/test_variable.py",
        ],
    },
    "xarray_2022_11_0_to_2022_12_0": {
        "python_version": "3.10",
        "test_files": [
            "xarray/tests/__init__.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_cftime_offsets.py",
            "xarray/tests/test_cftimeindex_resample.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_distributed.py",
            "xarray/tests/test_extensions.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_plugins.py",
            "xarray/tests/test_utils.py",
            "xarray/tests/test_variable.py",
        ],
    },
    "xarray_2023_04_0_to_2023_04_1": {
        "python_version": "3.10",
        "test_files": [
            "xarray/tests/test_concat.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_variable.py",
        ],
    },
    "xarray_2023_04_1_to_2023_04_2": {
        "python_version": "3.10",
        "test_files": [
            "xarray/tests/test_groupby.py",
        ],
    },
    "xarray_2025_01_0_to_2025_01_1": {
        "python_version": "3.12",
        "test_files": [
            "xarray/tests/test_backends.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_conventions.py",
            "xarray/tests/test_dataarray.py",
        ],
    },
    "xarray_2025_01_1_to_2025_01_2": {
        "python_version": "3.12",
        "test_files": [
            "xarray/tests/__init__.py",
            "xarray/tests/conftest.py",
            "xarray/tests/test_accessor_dt.py",
            "xarray/tests/test_backends.py",
            "xarray/tests/test_backends_datatree.py",
            "xarray/tests/test_cftime_offsets.py",
            "xarray/tests/test_cftimeindex.py",
            "xarray/tests/test_cftimeindex_resample.py",
            "xarray/tests/test_coding_times.py",
            "xarray/tests/test_combine.py",
            "xarray/tests/test_computation.py",
            "xarray/tests/test_concat.py",
            "xarray/tests/test_conventions.py",
            "xarray/tests/test_dataarray.py",
            "xarray/tests/test_dataset.py",
            "xarray/tests/test_datatree.py",
            "xarray/tests/test_distributed.py",
            "xarray/tests/test_duck_array_ops.py",
            "xarray/tests/test_groupby.py",
            "xarray/tests/test_interp.py",
            "xarray/tests/test_missing.py",
            "xarray/tests/test_namedarray.py",
            "xarray/tests/test_plot.py",
            "xarray/tests/test_variable.py",
        ],
    },
}

# Python version mapping
VERSION_TO_PYTHON = {
    "3.6": "base-py36",
    "3.8": "base-py38",
    "3.10": "base-py310",
    "3.12": "base-py312",
}


def _get_config(pr):
    """Get config for this PR's number_interval."""
    return PR_CONFIG.get(pr.number_interval, {
        "python_version": "3.12",
        "test_files": ["xarray/tests/"],
    })


def _get_version_deps(python_version, number_interval):
    """Return version-specific pip install lines for dockerfile."""
    m = re.match(r"xarray_(\d+)_(\d+)_", number_interval)
    if not m:
        return ""
    major, minor = int(m.group(1)), int(m.group(2))

    lines = []
    # Core test deps
    lines.append("RUN pip install h5py h5netcdf netCDF4 scipy || true")

    if major == 0 and minor <= 12:
        lines.append('RUN pip install "pandas==0.23.4" || pip install "pandas<0.25" || pip install pandas || true')
        lines.append('RUN pip install "numpy<1.24" || true')
    elif major == 0 and 14 <= minor <= 16:
        lines.append('RUN pip install "pandas<2.0" || true')
        lines.append('RUN pip install "numpy<2" || true')
        lines.append('RUN pip install "distributed<2022" || true')
    elif major == 0 and 18 <= minor <= 21:
        lines.append('RUN pip install "numpy<2" || true')
        lines.append('RUN pip install "pandas<2.0" || true')
        lines.append("RUN pip install setuptools || true")
    elif 2022 <= major <= 2023:
        lines.append('RUN pip install "numpy<2" || true')
        lines.append('RUN pip install "pandas<2.0" || true')

    if major == 0 and 10 <= minor <= 11:
        lines.append('RUN pip install "hypothesis<6" || true')
    if major == 0 and 10 <= minor <= 12:
        lines.append('RUN pip install "dask==0.19.4" "distributed<2.0" || true')
    if major >= 2025:
        lines.append("RUN pip install pytest-mypy-plugins mypy || true")
    if major >= 2024 or (major == 0 and minor >= 18) or (2022 <= major <= 2023):
        lines.append("RUN pip install pytz || true")

    lines.append("RUN pip install matplotlib bottleneck || true")
    lines.append('RUN pip install "dask[complete]" toolz || pip install dask toolz || true')

    return "\n".join(lines)


class ImageBase(Image):
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
        cfg = _get_config(self.pr)
        return f"python:{cfg['python_version']}-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        cfg = _get_config(self.pr)
        py_ver = str(cfg["python_version"])
        return VERSION_TO_PYTHON[py_ver]

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        cfg = _get_config(self.pr)
        return """FROM python:{python_version}-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}
""".format(
            python_version=cfg["python_version"],
            org=self.pr.org,
            repo=self.pr.repo,
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
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        cfg = _get_config(self.pr)
        test_files_str = " ".join(cfg["test_files"])

        run_sh = (
            "#!/bin/bash\n"
            "cd /home/xarray\n"
            'EXISTING_FILES=""\n'
            f"for f in {test_files_str}; do\n"
            '    if [ -f "$f" ]; then\n'
            '        EXISTING_FILES="$EXISTING_FILES $f"\n'
            "    fi\n"
            "done\n"
            'if [ -z "$EXISTING_FILES" ]; then\n'
            '    echo "No test files found at base commit, running xarray/tests/"\n'
            "    pytest -rA --timeout=300 xarray/tests/ -x -q 2>&1 || true\n"
            "else\n"
            "    pytest -rA --timeout=300 $EXISTING_FILES 2>&1 || true\n"
            "fi\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            f"cd /home/{self.pr.repo}\n"
            f"git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch || echo 'Warning: test patch apply had issues'\n"
            f"pytest -rA --timeout=300 {test_files_str}\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            f"cd /home/{self.pr.repo}\n"
            f"git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch || echo 'Warning: patch apply had issues'\n"
            f"pytest -rA --timeout=300 {test_files_str}\n"
        )

        prepare_sh = (
            "ls\n"
            "###ACTION_DELIMITER###\n"
            "\n"
            "###ACTION_DELIMITER###\n"
            "apt-get update && apt-get install -y build-essential\n"
            "###ACTION_DELIMITER###\n"
            "pip install --upgrade pip setuptools wheel\n"
            "###ACTION_DELIMITER###\n"
            'pip install -e ".[complete,test]" || pip install -e ".[test]" || pip install -e . || pip install -e ".[dev]"\n'
            "###ACTION_DELIMITER###\n"
            "pip install pytest pytest-xdist pytest-timeout\n"
            "###ACTION_DELIMITER###\n"
            f"echo 'pytest -rA --timeout=300 {test_files_str}' > test_commands.sh\n"
            "###ACTION_DELIMITER###\n"
            "bash test_commands.sh"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        dep_ref = f"{dep.image_name()}:{dep.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        version_deps = _get_version_deps(
            _get_config(self.pr)["python_version"],
            self.pr.number_interval,
        )

        return f"""FROM {dep_ref}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN pip install --upgrade pip setuptools wheel
{version_deps}
RUN pip install -e ".[complete,test]" || pip install -e ".[test]" || pip install -e . || pip install -e ".[dev]"
RUN pip install pytest pytest-xdist pytest-timeout || true

{copy_commands}
"""


def _parse_log(log: str) -> TestResult:
    """Shared parse_log for all xarray instances."""
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    test_status_pattern = re.compile(
        r"((xarray/\S*?\.py::\S+|properties/\S*?\.py::\S+))\s+(PASSED|FAILED|SKIPPED|XFAILED|XPASSED|ERROR)"
        r"|"
        r"(PASSED|FAILED|SKIPPED|XFAILED|XPASSED|ERROR)\s+((xarray/\S*?\.py::\S+|properties/\S*?\.py::\S+))"
    )

    test_status = {}
    for line in log.splitlines():
        stripped = ansi_escape.sub("", line)
        match = test_status_pattern.search(stripped)
        if match:
            test_name = match.group(1) if match.group(1) else match.group(5)
            status = match.group(3) if match.group(3) else match.group(4)
            test_status[test_name] = status

    passed = {t for t, s in test_status.items() if s in ("PASSED", "XPASSED")}
    failed = {t for t, s in test_status.items() if s in ("FAILED", "XFAILED", "ERROR")}
    skipped = {t for t, s in test_status.items() if s == "SKIPPED"}

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


# Programmatically register all 12 PRs
def _make_instance_class(ni):
    class _XarrayInstance(Instance):
        def __init__(self, pr, config, *args, **kwargs):
            super().__init__()
            self._pr = pr
            self._config = config

        @property
        def pr(self):
            return self._pr

        def dependency(self):
            return ImageDefault(self.pr, self._config)

        def run(self, run_cmd=""):
            if run_cmd:
                return run_cmd
            return "bash /home/run.sh"

        def test_patch_run(self, test_patch_run_cmd=""):
            if test_patch_run_cmd:
                return test_patch_run_cmd
            return "bash /home/test-run.sh"

        def fix_patch_run(self, fix_patch_run_cmd=""):
            if fix_patch_run_cmd:
                return fix_patch_run_cmd
            return "bash /home/fix-run.sh"

        def parse_log(self, test_log):
            return _parse_log(test_log)

    _XarrayInstance.__name__ = ni.upper()
    _XarrayInstance.__qualname__ = ni.upper()
    return _XarrayInstance


for _ni in PR_CONFIG:
    Instance.register("pydata", _ni)(_make_instance_class(_ni))
