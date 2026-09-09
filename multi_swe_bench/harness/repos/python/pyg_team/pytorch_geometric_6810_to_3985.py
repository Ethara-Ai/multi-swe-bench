import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NETWORK_TEST_PATTERN = (
    "onlyOnline|from_pretrained|download_url|extract_zip|extract_tar|extract_gz|"
    "Planetoid|TUDataset|SNAPDataset|SuiteSparseMatrixCollection|"
    "EllipticBitcoin|Reddit|Flickr|Entities|MovieLens|WikiCS|Coauthor|"
    "AmazonProducts|LightningDataModule|profileit|get_home_dir"
)

RESOLVE_NETWORK_IGNORES = """\
grep -rlE '{pattern}' test/ 2>/dev/null | sort -u > /opt/net_all.txt
grep -oE '^\\+\\+\\+ b/[^[:space:]]+' /home/test.patch | sed 's|^+++ b/||' | sort -u > /opt/net_graded.txt
comm -23 /opt/net_all.txt /opt/net_graded.txt > /opt/network_ignores.txt
""".format(pattern=NETWORK_TEST_PATTERN)

TEST_COMMAND = (
    "IGNORE_FLAGS=$(sed 's|^|--ignore=|' /opt/network_ignores.txt | tr '\\n' ' ')\n"
    "python -m pytest -o addopts= -v -p no:cacheprovider "
    "--continue-on-collection-errors \\\n    "
    "$IGNORE_FLAGS \\\n    "
    "test/"
)


PYPROJECT_HIDE = r"""if [ -f pyproject.toml ] && grep -q '^\[project\]' pyproject.toml && ! grep -q '^requires-python' pyproject.toml; then
  mv pyproject.toml /tmp/pyproject.toml.hidden
fi"""

PYPROJECT_RESTORE = r"""if [ -f /tmp/pyproject.toml.hidden ]; then
  mv /tmp/pyproject.toml.hidden pyproject.toml
fi"""


_ENV_SETUP = r"""printf 'torch==1.13.0\nnumpy<2\nsetuptools<81\nnetworkx<3\n' > /opt/constraints.txt

python -m pip install --no-cache-dir --upgrade pip wheel || true

python -m pip install --no-cache-dir --upgrade "setuptools<81" || true

python -m pip install --no-cache-dir torch==1.13.0 \
    --index-url https://download.pytorch.org/whl/cpu || true

MAX_JOBS=2 python -m pip install --no-cache-dir -c /opt/constraints.txt \
    --no-build-isolation torch-scatter==2.1.1 torch-sparse==0.6.17 || true

python -m pip install --no-cache-dir -c /opt/constraints.txt \
    networkx captum pandas torchmetrics==0.11.0 || true"""


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
        return "python:3.10"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-6810_to_3985"

    def workdir(self) -> str:
        return "base-6810_to_3985"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return []

    def dockerfile(self) -> str:
        packages = " \\\n    ".join(
            [
                "ca-certificates",
                "curl",
                "build-essential",
                "git",
                "gnupg",
                "make",
                "python3",
                "sudo",
                "wget",
            ]
        )

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {self.dependency()}",
            DockerfileEnhancer._infrastructure_block(
                self, self.dependency()
            ).rstrip("\n"),
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(
            "ENV OMP_NUM_THREADS=1\n"
            "ENV OPENBLAS_NUM_THREADS=1\n"
            "ENV MKL_NUM_THREADS=1"
        )
        sections.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        sections.append('RUN git clone "${REPO_URL}" /home/pytorch_geometric')

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"

    def extra_setup(self) -> str:
        return ""


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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh

{env_setup}

{resolve}

{pyproject_hide}

python -m pip install --no-cache-dir -c /opt/constraints.txt -e ".[test]" || true

{pyproject_restore}

python -m pip install --no-cache-dir -c /opt/constraints.txt class-resolver || true

python -m pip install --no-cache-dir -c /opt/constraints.txt yacs hydra-core rdflib tabulate matplotlib h5py || true

python -c "import pkg_resources, torch, torch_scatter, torch_sparse, torchmetrics, torch_geometric; print('env ok', torch.__version__, torchmetrics.__version__, torch_geometric.__version__)"

bash /home/check_git_changes.sh

""".format(repo=repo, env_setup=_ENV_SETUP, resolve=RESOLVE_NETWORK_IGNORES, pyproject_hide=PYPROJECT_HIDE, pyproject_restore=PYPROJECT_RESTORE),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}

""".format(repo=repo, test_command=TEST_COMMAND),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        sections = [f"FROM {name}:{tag}"]

        if self.global_env:
            sections.append(self.global_env)

        sections.append(copy_commands.rstrip("\n"))
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(hardening)
        sections.append("RUN bash /home/prepare.sh")

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("pyg-team", "pytorch_geometric_6810_to_3985")
class PYTORCH_GEOMETRIC_6810_TO_3985(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        line_re = re.compile(
            r"^(?P<name>\S.*?::.+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b.*$"
        )

        for raw_line in log.split("\n"):
            match = line_re.match(raw_line.rstrip())
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("pyg-team", "3985-4957-6222-6540-6810")(
    PYTORCH_GEOMETRIC_6810_TO_3985
)
