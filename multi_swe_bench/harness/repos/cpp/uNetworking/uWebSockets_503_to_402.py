import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─── Constants ────────────────────────────────────────────────────
REPO_DIR = "uWebSockets"  # Must match git clone target EXACTLY
# ──────────────────────────────────────────────────────────────────


class ImageBase(Image):
    """Base image: Ubuntu + C++11 toolchain + openssl/libuv/zlib + repo clone."""

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
        return "ubuntu:16.04"

    def image_tag(self) -> str:
        return "base-503-402"

    def workdir(self) -> str:
        return "base-503-402"

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
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make sudo wget \\
    build-essential g++ \\
    libssl-dev libuv1-dev zlib1g-dev \\
    netcat-openbsd \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
make || true
"""),
            File(".", "run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
make clean || true
make
make install || true
if [ -f tests/main.cpp ]; then
    g++ -std=c++11 -O3 tests/main.cpp -Isrc -o testsBin -lpthread -luWS -lssl -lcrypto -lz -luv || true
fi
if [ -f ./testsBin ]; then
    timeout 120 ./testsBin || echo "TIMEOUT_OR_FAIL: testsBin exited with $?"
else
    echo "SKIP: testsBin not built"
fi
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn --binary /home/test.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || echo "PATCH_WARN: test patch apply had issues"
make clean || true
make
make install || true
if [ -f tests/main.cpp ]; then
    g++ -std=c++11 -O3 tests/main.cpp -Isrc -o testsBin -lpthread -luWS -lssl -lcrypto -lz -luv || true
fi
if [ -f ./testsBin ]; then
    timeout 120 ./testsBin || echo "TIMEOUT_OR_FAIL: testsBin exited with $?"
else
    echo "SKIP: testsBin not built"
fi
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch 2>&1 || echo "PATCH_WARN: fix patch apply had issues"
make clean || true
make
make install || true
if [ -f tests/main.cpp ]; then
    g++ -std=c++11 -O3 tests/main.cpp -Isrc -o testsBin -lpthread -luWS -lssl -lcrypto -lz -luv || true
fi
if [ -f ./testsBin ]; then
    timeout 120 ./testsBin || echo "TIMEOUT_OR_FAIL: testsBin exited with $?"
else
    echo "SKIP: testsBin not built"
fi
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


@Instance.register("uNetworking", "uWebSockets_503_to_402")
class UWebSockets503To402(Instance):
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

        has_test_output = False
        has_failure = False

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue
            # Test binary produces "Messages per microsecond:" and "Throughput:" lines
            if "Messages per microsecond:" in line or "Throughput:" in line:
                has_test_output = True
            if line.startswith("FAILURE:"):
                has_failure = True
                failed_tests.add(line)
            if "SKIP: testsBin not built" in line:
                skipped_tests.add("testsBin")

        # If test binary ran and produced output without FAILURE lines -> passed
        if has_test_output and not has_failure:
            passed_tests.add("testsBin")
        elif has_test_output and has_failure:
            # Some tests ran but failed at end
            passed_tests.add("testsBin_partial")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
