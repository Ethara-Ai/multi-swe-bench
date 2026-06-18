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
        # Returning a string lets the base Image.dockerfile() build the clone,
        # the ${BASE_COMMIT} checkout and the hardening block, and lets the
        # DockerfileEnhancer inject proxy/cert/infra (per image.py).
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # Include the base commit so each PR's base image is pinned correctly.
        # The hardening block in image.py detaches to ${BASE_COMMIT} and strips
        # all refs/remotes; a per-sha tag prevents a shared base image from being
        # pinned to a single PR's commit and breaking the others.
        sha = self.pr.base.sha[:8] if getattr(self.pr.base, "sha", None) else "base"
        return f"base-old-{sha}"

    def workdir(self) -> str:
        # Keep the build context dir per-sha as well so concurrent base builds
        # for different PRs do not overwrite each other's Dockerfile.
        sha = self.pr.base.sha[:8] if getattr(self.pr.base, "sha", None) else "base"
        return f"base-old-{sha}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # build-essential (from the default packages) already provides gcc/g++;
        # these are the extra headers locust's native deps (pyzmq, gevent, lxml)
        # need to build.
        return [
            "g++",
            "python3-dev",
            "libzmq3-dev",
            "libev-dev",
            "libxml2-dev",
            "libxslt-dev",
        ]

    def extra_setup(self) -> str:
        # Runs in WORKDIR /home/locust after the ${BASE_COMMIT} checkout and
        # before the hardening block, so the editable install is wired to the
        # pinned source and git history is still present.
        return (
            "RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel\n"
            'RUN python -m pip install --no-cache-dir "gevent<23" "greenlet<3"\n'
            'RUN python -m pip install --no-cache-dir -e ".[dev]" '
            "|| python -m pip install --no-cache-dir -e .\n"
            "RUN python -m pip install --no-cache-dir "
            "pytest mock pyquery pytest-timeout cryptography"
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

{self.clear_env}

CMD ["/bin/bash"]
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
