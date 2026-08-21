import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """PR-independent layer: system packages + a full clone of the repo.

    Everything here is identical for every augur PR in the 432..598 range, so
    it is cached once and reused instead of being re-run per PR.
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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.9-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # Tagged `base-pr-<number>` rather than a shared `base`: the Dockerfile QC
        # contract requires the PR layer to inherit
        # `mswebench/<org>_m_<repo>:base-pr-<N>`, and a shared tag hides a real
        # hazard -- this image bakes in one BASE_COMMIT, so a reused `base` stays
        # pinned to whichever PR built it first and any later PR whose base commit
        # is unreachable from that sha dies in prepare.sh. Costs one base image per
        # PR instead of one per repo; deliberate.
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

# git clones the repo here and applies patches at test time. vcftools is invoked
# as a real binary by augur/filter.py (via which("vcftools")); without it four
# tests in tests/test_filter.py fail. build-essential backs source builds of the
# pinned dependency set installed in the PR layer.
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        build-essential \\
        vcftools \\
 && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


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

    def dependency(self) -> Union[str, "Image"]:
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euxo pipefail

cd /home/[[REPO_NAME]]

# Re-assert the pinned baseline at PR-build time rather than trusting the base
# layer's checkout to still hold. Without this the graded stages inherit whatever
# state the tree happens to be in, and a leftover patch would silently be tested
# instead of the real base commit.
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh

# The repo's environment.yml pins python=3.6 from the `defaults` conda channel,
# which has no linux-aarch64 builds and is long unsupported. Install the same
# dependency set with pip on the image's own Python 3.9 instead.
# setuptools must stay <81: bcbio-gff (a hard install_requires at this commit)
# uses a legacy setup.py that imports pkg_resources, which setuptools 81 removed.
# augur/utils.py imports pkg_resources at runtime for the same reason.
python -m pip install --no-cache-dir "setuptools<81" "wheel<0.46"
python -m pip install --no-cache-dir -e '.[dev]'

# Two pins the 2020-era setup.py does not constrain, both required at this commit:
#   numpy<1.23      - pandas 1.5.x wheels are built against the numpy 1.22 C API;
#                     a newer numpy raises "numpy.dtype size changed" on import.
#   biopython==1.76 - augur/align.py:337 uses Seq(..., alphabet=...), removed in
#                     Biopython 1.78 (test_align.py::test_make_gaps_ambiguous).
python -m pip install --no-cache-dir "numpy<1.23" "biopython==1.76"

python -c "import augur.filter, augur.utils, pandas, numpy, Bio"
""".replace("[[REPO_NAME]]", repo_name).replace(
                    "[[BASE_SHA]]", self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
pytest -p no:cacheprovider -rA --tb=no -q -c pytest.python3.ini tests/
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -p no:cacheprovider -rA --tb=no -q -c pytest.python3.ini tests/
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -p no:cacheprovider -rA --tb=no -q -c pytest.python3.ini tests/
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The base layer already clones the repo, checks out pr.base.sha detached
        # and asserts HEAD == BASE_COMMIT, so no ENV/checkout is repeated here.
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("nextstrain", "augur_598_to_432")
class AUGUR_598_TO_432(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests: set[str] = set()  # Tests that passed successfully
        failed_tests: set[str] = set()  # Tests that failed
        skipped_tests: set[str] = set()  # Tests that were skipped

        # pytest.python3.ini forces `-s`, so captured stdout from the code under
        # test is interleaved with the progress output and a status can land on
        # the same line as unrelated text. The `-rA` short summary at the end of
        # the run is the reliable source: one "STATUS test::id" line per test.
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)",
            re.MULTILINE,
        )
        # Fallback for the verbose "test::id STATUS" form, anchored on a nodeid
        # so interleaved stdout cannot be mistaken for a test name.
        verbose_pattern = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)",
            re.MULTILINE,
        )

        results: dict[str, str] = {}
        for status, test_name in summary_pattern.findall(log):
            results[test_name] = status
        for test_name, status in verbose_pattern.findall(log):
            results.setdefault(test_name, status)

        for test_name, status in results.items():
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
