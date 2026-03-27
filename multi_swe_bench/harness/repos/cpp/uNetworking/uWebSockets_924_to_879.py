import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ─── Constants ────────────────────────────────────────────────────
REPO_DIR = "uWebSockets"  # Must match git clone target EXACTLY
# ──────────────────────────────────────────────────────────────────


class ImageBase(Image):
    """Base image: Ubuntu + clang + llvm-10 (LTO) + C++17 + uSockets submodule + openssl/zlib + repo clone."""

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
        return "base-924-879"

    def workdir(self) -> str:
        return "base-924-879"

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
    build-essential clang llvm-10 \\
    libssl-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

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
cd /home/{REPO_DIR}
# Build uSockets
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
make -C uSockets 2>&1
echo "=== uSockets build done ==="
# Build main project (examples target) — may fail on -flto, that's OK
make 2>&1 || echo "=== make failed (expected if -flto LTO issue) ==="
# If tests/ directory exists (PR 924), run those tests
if [ -d "tests" ] && [ -f "tests/Makefile" ]; then
    echo "=== Running unit tests ==="
    cd tests
    make clean 2>/dev/null || true
    make 2>&1
else
    echo "=== No tests/ directory ==="
    # For PR 879: build benchmarks if they exist
    if [ -d "benchmarks" ]; then
        echo "=== Building benchmarks ==="
        if [ -f "benchmarks/Makefile" ]; then
            make -C benchmarks 2>&1 || echo "=== benchmarks build failed ==="
        fi
    fi
    echo "=== Build completed ==="
fi
"""),
            File(".", "test-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
# Apply test patch (may fail partially on binary files)
git apply --whitespace=nowarn --binary /home/test.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || echo "=== test patch apply had issues ==="
# Build uSockets
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
make -C uSockets 2>&1
echo "=== uSockets build done ==="
make 2>&1 || echo "=== make failed (expected if -flto LTO issue) ==="
if [ -d "tests" ] && [ -f "tests/Makefile" ]; then
    echo "=== Running unit tests ==="
    cd tests
    make clean 2>/dev/null || true
    make 2>&1
else
    if [ -d "benchmarks" ]; then
        echo "=== Building benchmarks ==="
        if [ -f "benchmarks/Makefile" ]; then
            make -C benchmarks 2>&1 || echo "=== benchmarks build failed ==="
        fi
    fi
    echo "=== Build completed ==="
fi
"""),
            File(".", "fix-run.sh", f"""#!/bin/bash
cd /home/{REPO_DIR}
git apply --whitespace=nowarn --binary /home/test.patch 2>&1 || git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || echo "=== test patch apply had issues ==="
git apply --whitespace=nowarn --binary /home/fix.patch 2>&1 || git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || echo "=== fix patch apply had issues ==="
# Build uSockets
make -C uSockets clean 2>/dev/null || true
make clean 2>/dev/null || true
make -C uSockets 2>&1
echo "=== uSockets build done ==="
make 2>&1 || echo "=== make failed (expected if -flto LTO issue) ==="
if [ -d "tests" ] && [ -f "tests/Makefile" ]; then
    echo "=== Running unit tests ==="
    cd tests
    make clean 2>/dev/null || true
    make 2>&1
else
    if [ -d "benchmarks" ]; then
        echo "=== Building benchmarks ==="
        if [ -f "benchmarks/Makefile" ]; then
            make -C benchmarks 2>&1 || echo "=== benchmarks build failed ==="
        fi
    fi
    echo "=== Build completed ==="
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


@Instance.register("uNetworking", "uWebSockets_924_to_879")
class UWebSockets924To879(Instance):
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

        test_names = [
            "TopicTree", "BloomFilter", "HttpRouter", "ExtensionsNegotiator"
        ]

        lines = test_log.splitlines()
        for name in test_names:
            compile_found = False
            compile_error = False
            for i, line in enumerate(lines):
                if f"-o {name}" in line and (f"{name}.cpp" in line or f"g++" in line):
                    compile_found = True
                    for j in range(i + 1, min(i + 20, len(lines))):
                        if "error:" in lines[j] and name in lines[j]:
                            compile_error = True
                            break
                        if "fatal error:" in lines[j]:
                            compile_error = True
                            break
                        if f"-o " in lines[j] and any(n in lines[j] for n in test_names if n != name):
                            break
                    break
            if compile_found and not compile_error:
                passed_tests.add(name)
            elif compile_found and compile_error:
                failed_tests.add(name)

        if not any(name in passed_tests or name in failed_tests for name in test_names):
            build_error = False
            for line in lines:
                line_s = line.strip()
                if "error:" in line_s and ("linker command failed" in line_s or "ld:" in line_s):
                    build_error = True
                    failed_tests.add("link")
                if "make: ***" in line_s and "Error" in line_s:
                    build_error = True
                    failed_tests.add("build")
            if not build_error:
                passed_tests.add("build")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
