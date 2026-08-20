import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PanderaImageBase(Image):
    """Repo-level base (`images/base/`, tag `:base`).

    Deliberately does NOT override `dockerfile()`: the default in
    `Image.dockerfile()` (harness/image.py:200) emits the canonical
    FROM + apt + `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` +
    hardening sequence, and `DockerfileEnhancer` injects TARGETARCH /
    proxy args / cert symlinks / multi-arch labels on top. Overriding
    here bypasses that and breaks multi-arch buildx / OCI export.
    """

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
        # python:3.9-slim matches pandera's Dec-2021 era at base.sha
        # fed2a47. numpy==1.21.6 + pandas==1.3.5 (pinned in prepare.sh)
        # have cp39 manylinux wheels for amd64 AND arm64, so no
        # from-source BLAS compile. Python 3.10+ was not yet supported
        # by pandera at this commit (typing.Generic runtime changes in
        # 3.10 break pandera.typing generics).
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def extra_packages(self) -> list[str]:
        # default_packages (harness/image.py:207) already provides
        # ca-certificates, curl, build-essential, git, gnupg, make,
        # python3, sudo, wget. pandera's test surface at fed2a47 is
        # pure-Python (pandas/numpy/hypothesis/pyarrow all install
        # from wheels for cp39 on amd64+arm64), so no extras needed.
        return []

    def files(self) -> list[File]:
        return []


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
        return PanderaImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                "prepare.sh",
                """#!/bin/bash
set -eux

cd /home/pandera
pip install -U -q pip

# Pin the Dec-2021-era pandas/numpy stack. requirements-dev.txt at
# base.sha fed2a47 uses UNPINNED `pandas` and `numpy`; if pip resolves
# to latest (pandas>=2, numpy>=2) this pandera source fails because:
#   - Series.append was removed in pandas 2.0 (used in schema.py)
#   - np.compat.long was removed in numpy 2.0 (used via pandera.dtypes)
#   - dtype inference semantics changed in pandas 2.0
# numpy==1.21.6 + pandas==1.3.5 are the last contemporary releases
# with base.sha (Dec 2021) and both have manylinux wheels for cp39 on
# amd64+arm64, so no from-source BLAS/pandas compile is needed.
# pandas-stubs is a test-only mypy dep (pandera imports it at test
# collection time via tests/core/test_model.py).
pip install -q \\
    'numpy==1.21.6' 'pandas==1.3.5' \\
    hypothesis pyyaml pyarrow typing_inspect wrapt pydantic packaging \\
    pandas-stubs pytest pytest-asyncio
pip install -q -e .

# Runner script called by run.sh / test-run.sh / fix-run.sh after any
# patch apply. Scoped to tests/core/{test_model,test_schemas}.py — the
# two files test.patch touches with executable pytest tests. Three
# other files in test.patch are intentionally skipped:
#   - .github/workflows/ci-tests.yml (not a test, just CI config)
#   - tests/mypy/test_static_type_checking.py (needs full mypy env +
#     stubs; assertions orthogonal to PR #758's runtime unique-column-
#     names feature; adds ~5 min per stage and flakes on ARM)
# --no-header -rA --tb=no -p no:cacheprovider -v are load-bearing for
# parse_log's regex anchors below; do NOT change flags without
# updating parse_log's passed_pattern/failed_pattern/skipped_pattern.
cat > /home/pandera/test_commands.sh <<'EOF'
#!/bin/bash
cd /home/pandera
pytest tests/core/test_model.py tests/core/test_schemas.py \\
    --no-header -rA --tb=no -p no:cacheprovider -v
EOF
chmod +x /home/pandera/test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/pandera
bash /home/pandera/test_commands.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/pandera
if ! git -C /home/pandera apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/pandera/test_commands.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/pandera
if ! git -C /home/pandera apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/pandera/test_commands.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("unionai-oss", "pandera_1925_to_306")
class PANDERA_1925_TO_306(Instance):
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

        # pytest -v --no-header -rA output shapes at this repo:
        #   Progress line:  "tests/core/test_x.py::test_y[param] PASSED [ 12%]"
        #   -rA summary:    "PASSED tests/core/test_x.py::test_y[param]"
        #                   "FAILED tests/core/test_x.py::test_y - TypeError: ..."
        #                   "SKIPPED [1] tests/core/test_x.py:12: reason"
        # passed_pattern uses `search` from anywhere on the line so it
        # matches BOTH the progress line ("tests/... PASSED") and the
        # -rA summary line ("PASSED tests/..."). The optional
        # `\[\s*\d+%\]` handles the progress form only. failed_pattern
        # anchors on `FAILED ` then captures test id up to ` -` (which
        # `--tb=no` still emits for the -rA summary) or end of line.
        # skipped_pattern handles both orderings ("SKIPPED [n] path:"
        # and "path SKIPPED"). Do NOT change --no-header / -rA / -v
        # flags without also updating these three regexes.
        passed_pattern = re.compile(r"(tests/.*?)\s+PASSED(?:\s+\[\s*\d+%\])?")
        failed_pattern = re.compile(r"FAILED\s+(tests/.*?)(?:\s+-|$)")
        skipped_pattern = re.compile(r"(SKIPPED\s+(tests/.*?)|(tests/.*?)\s+SKIPPED)")

        for raw in log.split("\n"):
            line = raw.strip()
            m = passed_pattern.search(line)
            if m:
                passed_tests.add(m.group(1))
            m = failed_pattern.search(line)
            if m:
                failed_tests.add(m.group(1))
            m = skipped_pattern.search(line)
            if m:
                name = m.group(2) or m.group(3)
                if name:
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
