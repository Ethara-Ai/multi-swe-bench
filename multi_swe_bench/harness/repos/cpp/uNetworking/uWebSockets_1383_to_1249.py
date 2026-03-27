import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─── Constants ────────────────────────────────────────────────────
REPO_DIR = "uWebSockets"  # Must match git clone target EXACTLY
# ──────────────────────────────────────────────────────────────────


class ImageBase(Image):
    """Base image: Ubuntu 20.04 + C++20 toolchain + uSockets submodule + zlib + repo clone."""

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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "base-1383-1249"

    def workdir(self) -> str:
        return "base-1383-1249"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone --recurse-submodules https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make sudo wget \\
    build-essential g++-10 \\
    libssl-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-10 100 \\
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-10 100 \\
    && update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-10 100 \\
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-10 100

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}
RUN git submodule update --init --recursive

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR image: based on ImageBase, adds patches + prepare."""

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

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", """#!/bin/bash
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
"""),
            File(".", "prepare.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git submodule deinit --all -f || true
git checkout -- . || true
git clean -fdx || true
git reset --hard
git checkout {self.pr.base.sha}
git submodule update --init --recursive
make -C uSockets || true
make || true
"""),
            File(".", "run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
make -C uSockets clean || true
make clean || true
make -C uSockets
make
cd tests
make clean || true
make
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch
make -C uSockets clean || true
make clean || true
make -C uSockets
make
cd tests
make clean || true
make
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
make -C uSockets clean || true
make clean || true
make -C uSockets
make
cd tests
make clean || true
make
"""),
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


@Instance.register("uNetworking", "uWebSockets_1383_to_1249")
class UWebSockets1383To1249(Instance):
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

        # tests/Makefile compiles and runs each test: g++ ... -o TestName && ./TestName
        test_names = [
            "TopicTree", "BloomFilter", "HttpRouter", "ExtensionsNegotiator"
        ]
        for name in test_names:
            compiled = False
            errored = False
            for line in test_log.splitlines():
                if f"-o {name}" in line:
                    compiled = True
                if compiled and ("error:" in line or "Error" in line or "*** [" in line):
                    if name in line or f"-o {name}" in line:
                        errored = True
            if compiled and not errored:
                passed_tests.add(name)
            elif compiled and errored:
                failed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
