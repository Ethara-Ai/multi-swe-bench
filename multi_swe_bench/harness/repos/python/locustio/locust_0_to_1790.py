from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "locust"


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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-old"

    def workdir(self) -> str:
        return "base-old"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make sudo wget build-essential \\
    gcc g++ python3-dev libzmq3-dev libev-dev \\
    libxml2-dev libxslt-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{self.clear_env}

CMD ["/bin/bash"]
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
pip install --upgrade pip setuptools wheel
pip install "gevent<23" "greenlet<3"
pip install -e ".[dev]" || pip install -e .
pip install pytest mock pyquery pytest-timeout cryptography
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
python -m pytest locust/test/ -v --timeout=60
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
python -m pytest locust/test/ -v --timeout=60
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python -m pytest locust/test/ -v --timeout=60
""",
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

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("locustio", "locust_0_to_1790")
class LOCUST_0_TO_1790(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for line in test_log.splitlines():
            line = line.strip()
            if " PASSED" in line:
                if "[" in line:
                    line = line.split("[")[0].strip()
                if line.endswith(" PASSED"):
                    test_name = line.rsplit(" PASSED", 1)[0].strip()
                    if test_name:
                        passed_tests.add(test_name)
            elif " FAILED" in line:
                if "[" in line:
                    line = line.split("[")[0].strip()
                if line.endswith(" FAILED"):
                    test_name = line.rsplit(" FAILED", 1)[0].strip()
                    if test_name:
                        failed_tests.add(test_name)
                elif line.startswith("FAILED "):
                    test_name = line[7:].strip()
                    if " - " in test_name:
                        test_name = test_name.split(" - ")[0].strip()
                    if test_name:
                        failed_tests.add(test_name)
            elif " SKIPPED" in line:
                if "[" in line:
                    line = line.split("[")[0].strip()
                test_name = line.split(" SKIPPED")[0].strip()
                if test_name:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
