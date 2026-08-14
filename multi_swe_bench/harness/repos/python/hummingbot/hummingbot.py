import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Ignore dirs mirror the project's `make test` target (broken/slow/network suites).
_IGNORES = (
    "--ignore=test/mock "
    "--ignore=test/hummingbot/remote_iface/ "
    "--ignore=test/connector/utilities/oms_connector/ "
    "--ignore=test/hummingbot/strategy/amm_arb/ "
    "--ignore=test/hummingbot/strategy/cross_exchange_market_making/"
)


class ImageBase(Image):
    """Repo-level base image: OS deps + cloned/checked-out source + conda env + Cython build.
    Built once as `<repo>:base`; PR images layer only patches + scripts on top."""

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
        return "continuumio/miniconda3:latest"

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

        # Clone via "${{REPO_URL}}" (the pipeline enhancer leaves this form untouched and
        # injects its git-hardening block just before our trailing CMD); the conda env +
        # Cython build run after checkout so they compile against the checked-out source.
        return f"""FROM {image_name}
ENV DEBIAN_FRONTEND=noninteractive
# build-essential needed to compile hummingbot's Cython extensions.
RUN apt-get update && apt-get install -y git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
# Base image `git clone` can land an incomplete packfile; fetch the base commit by URL if missing.
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}
RUN git checkout ${{BASE_COMMIT}}

# --- Environment baked in so human_mode=True works: conda env + Cython compile.
RUN conda env create -f setup/environment.yml
# environment.yml only pins python>=3.10.12, so conda picks 3.12 where the newest
# setuptools drops pkg_resources and a too-new xrpl-py loses require_kwargs_on_init.
# Pin both to what the code at this commit expects (setup.py: xrpl-py>=4.1.0).
RUN conda run -n hummingbot pip install --no-cache-dir "setuptools<81" "xrpl-py==4.1.0"
RUN conda run -n hummingbot python setup.py build_ext --inplace -j 8

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
        return ImageBase(self.pr, self._config)

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
# Re-assert the base commit at PR-build time. Non-destructive on purpose: no `git reset`
# (some bases carry intentional working-tree edits from their env setup) and no test run
# (tests execute at instance time via run/test/fix-run.sh).
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
cd /home/{pr.repo}
pytest -v -rA --continue-on-collection-errors test/hummingbot/connector/exchange/xrpl/
""".format(pr=self.pr, ignores=_IGNORES),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA --continue-on-collection-errors test/hummingbot/connector/exchange/xrpl/
""".format(pr=self.pr, ignores=_IGNORES),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
source /opt/conda/etc/profile.d/conda.sh
conda activate hummingbot
cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA --continue-on-collection-errors test/hummingbot/connector/exchange/xrpl/
""".format(pr=self.pr, ignores=_IGNORES),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("hummingbot", "hummingbot")
class Hummingbot(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in log.split("\n"):
            line = raw.strip()
            m = re.match(r"^(PASSED|FAILED|ERROR|SKIPPED)\s+(\S+)", line)
            if m:
                status, name = m.group(1), m.group(2)
            else:
                m = re.match(r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line)
                if not m:
                    continue
                name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
