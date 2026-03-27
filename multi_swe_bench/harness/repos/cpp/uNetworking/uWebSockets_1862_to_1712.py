import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─── Constants ────────────────────────────────────────────────────
REPO_DIR = "uWebSockets"  # Must match git clone target EXACTLY
# ──────────────────────────────────────────────────────────────────


class ImageBase(Image):
    """Base image: Ubuntu latest + C++20 + build.c meta-build + uSockets submodule + zlib + repo clone."""

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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base-1862-1712"

    def workdir(self) -> str:
        return "base-1862-1712"

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
    ca-certificates curl git gnupg make sudo wget unzip \\
    build-essential g++-12 cmake \\
    libssl-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/* \\
    && update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-12 100 \\
    && update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-12 100 \\
    && update-alternatives --install /usr/bin/cc cc /usr/bin/gcc-12 100 \\
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/g++-12 100
RUN curl -fsSL https://deno.land/install.sh | sh \\
    && ln -sf /root/.deno/bin/deno /usr/local/bin/deno

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
if [ -d "libdeflate" ]; then
    cmake -S libdeflate -B libdeflate/build || true
    make -C libdeflate/build || true
fi
make -C uSockets || true
make || true
"""),
            File(".", "run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
if [ -d "libdeflate" ] && [ -d "libdeflate/build" ]; then
    cmake -S libdeflate -B libdeflate/build 2>/dev/null || true
    make -C libdeflate/build 2>/dev/null || true
fi
make -C uSockets 2>&1
make 2>&1
./build examples 2>&1 || echo "BUILD_EXAMPLES_FAIL: $?"
echo "=== C++ unit tests ==="
cd tests
make clean 2>/dev/null || true
make 2>&1
cd /home/{REPO_DIR}
echo "=== Deno smoke tests ==="
if [ -f "Crc32" ] && [ -f "tests/smoke.mjs" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-all tests/smoke.mjs 2>&1 || echo "SMOKE_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
fi
if [ -f "Crc32" ] && [ -f "tests/http_test.ts" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-net tests/http_test.ts localhost 3000 2>&1 || echo "HTTP_TEST_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
fi
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn --binary /home/test.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || echo "PATCH_WARN: test patch apply had issues"
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
if [ -d "libdeflate" ] && [ -d "libdeflate/build" ]; then
    cmake -S libdeflate -B libdeflate/build 2>/dev/null || true
    make -C libdeflate/build 2>/dev/null || true
fi
make -C uSockets 2>&1
make 2>&1
./build examples 2>&1 || echo "BUILD_EXAMPLES_FAIL: $?"
echo "=== C++ unit tests ==="
cd tests
make clean 2>/dev/null || true
make 2>&1
cd /home/{REPO_DIR}
echo "=== Deno smoke tests ==="
if [ -f "Crc32" ] && [ -f "tests/smoke.mjs" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-all tests/smoke.mjs 2>&1 || echo "SMOKE_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
fi
if [ -f "Crc32" ] && [ -f "tests/http_test.ts" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-net tests/http_test.ts localhost 3000 2>&1 || echo "HTTP_TEST_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
fi
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn --binary /home/test.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || echo "PATCH_WARN: test patch apply had issues"
git apply --whitespace=nowarn --binary /home/fix.patch 2>&1 || git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || echo "PATCH_WARN: fix patch apply had issues"
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
if [ -d "libdeflate" ] && [ -d "libdeflate/build" ]; then
    cmake -S libdeflate -B libdeflate/build 2>/dev/null || true
    make -C libdeflate/build 2>/dev/null || true
fi
make -C uSockets 2>&1
make 2>&1
./build examples 2>&1 || echo "BUILD_EXAMPLES_FAIL: $?"
echo "=== C++ unit tests ==="
cd tests
make clean 2>/dev/null || true
make 2>&1
cd /home/{REPO_DIR}
echo "=== Deno smoke tests ==="
if [ -f "Crc32" ] && [ -f "tests/smoke.mjs" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-all tests/smoke.mjs 2>&1 || echo "SMOKE_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
fi
if [ -f "Crc32" ] && [ -f "tests/http_test.ts" ]; then
    ./Crc32 &
    SERVER_PID=$!
    sleep 2
    timeout 60 deno run --allow-net tests/http_test.ts localhost 3000 2>&1 || echo "HTTP_TEST_EXIT: $?"
    kill $SERVER_PID 2>/dev/null || true
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


@Instance.register("uNetworking", "uWebSockets_1862_to_1712")
class UWebSockets1862To1712(Instance):
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

        # --- C++ unit tests (compile-and-run pattern) ---
        test_names = [
            "TopicTree", "BloomFilter", "HttpRouter", "ExtensionsNegotiator",
            "HttpParser", "ChunkedEncoding", "Query"
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

        # --- Deno http_test.ts: "✅ Test name: details" / "❌ Test name: details" ---
        for line in test_log.splitlines():
            line_s = line.strip()
            if line_s.startswith("\u2705"):
                rest = line_s[1:].strip()
                colon_idx = rest.find(":")
                if colon_idx > 0:
                    passed_tests.add("http_" + rest[:colon_idx].strip())
            elif line_s.startswith("\u274c"):
                rest = line_s[1:].strip()
                colon_idx = rest.find(":")
                if colon_idx > 0:
                    failed_tests.add("http_" + rest[:colon_idx].strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
