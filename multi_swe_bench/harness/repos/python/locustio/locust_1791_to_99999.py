from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "locust"


class ImageBase(Image):
    """Level 1: toolchain-only base image (shared per Python-era).

    dependency() returns a *string* (the Python toolchain) and this Dockerfile
    carries NO ``# syntax`` directive, so the DockerfileEnhancer engages and
    prepends the ``# syntax``/ARG/ENV/LABEL infra block. IMPORTANT: this image
    must NOT clone the repository -- a shared string-dependency image that
    clones is force-pinned to a single ``${BASE_COMMIT}`` and history-stripped
    by the enhancer, breaking ``git checkout`` for every other PR sharing the
    base. So the clone lives in ImageDefault (an Image dependency, left verbatim
    by the enhancer), done per-PR. This image only provides the Python toolchain
    and apt build deps for locust's native deps (pyzmq, gevent, lxml).

    Two eras: PRs < 2500 need Python 3.8 (pyzmq compat), else Python 3.11. The
    base tag is per-era (base-py38 / base-py311), shared across all PRs of that
    era -- not per-sha, since the base no longer clones or pins anything.
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
        if self._pr.number < 2500:
            return "python:3.8-slim"
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-py38" if self._pr.number < 2500 else "base-py311"

    def workdir(self) -> str:
        return "base-py38" if self._pr.number < 2500 else "base-py311"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        # No `git clone` here on purpose (see docstring); no `# syntax` directive
        # either, so the DockerfileEnhancer injects the ARG/ENV/LABEL infra block
        # (but no clone/hardening, since this Dockerfile has no clone).
        return f"""FROM {image_name}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git build-essential g++ python3-dev \\
    libzmq3-dev libev-dev libxml2-dev libxslt-dev \\
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel

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

    def _install_sh(self) -> str:
        # Editable install of the pinned source + test deps. Runs in the per-PR
        # image AFTER the clone + ${BASE_COMMIT} checkout and BEFORE the
        # hardening strip (full clone still present), mirroring the template's
        # `RUN bash /home/install.sh`.
        lines = [
            "#!/bin/bash",
            "set -e",
            f"cd /home/{REPO_DIR}",
            "python -m pip install --no-cache-dir --upgrade pip setuptools wheel",
        ]
        if self._pr.number < 2500:
            lines.append(
                'python -m pip install --no-cache-dir "gevent<23" "greenlet<3"'
            )
        lines.append(
            'python -m pip install --no-cache-dir -e ".[dev]" '
            '|| python -m pip install --no-cache-dir -e "."'
        )
        lines.append(
            "python -m pip install --no-cache-dir "
            "pytest pytest-timeout mock pyquery cryptography retry"
        )
        return "\n".join(lines) + "\n"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", self._install_sh()),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
python -m pytest locust/test/ -v --timeout=120 --timeout-method=thread
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
python -m pytest locust/test/ -v --timeout=120 --timeout-method=thread
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python -m pytest locust/test/ -v --timeout=120 --timeout-method=thread
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Single COPY of all scripts/patches into /home/ (inline template style).
        copy_files = " ".join(file.name for file in self.files())

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first, then checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written
        # (and pinning here is correct: it is per-PR, not the shared base).
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

COPY {copy_files} /home/

RUN bash /home/install.sh || true

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK
        # (detach at ${BASE_COMMIT}, remove origin, delete all refs, reflog
        # expire, gc/repack, drop alternates, + asserts, then submodule strip).
        # Concatenated raw (not via f-string) so its ${BASE_COMMIT} / %(refname)
        # tokens stay literal.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("locustio", "locust_1791_to_99999")
class LOCUST_1791_TO_99999(Instance):
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
