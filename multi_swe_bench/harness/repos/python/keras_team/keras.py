import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.11-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                "filter_patch.py",
                (
                    "import re, sys\n"
                    "\n"
                    "if len(sys.argv) < 2:\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "try:\n"
                    "    content = open(sys.argv[1]).read()\n"
                    "except (IOError, OSError):\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "if not content.strip():\n"
                    "    sys.exit(1)\n"
                    "\n"
                    "parts = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)\n"
                    "filtered = [p for p in parts if p.strip() and 'Binary files' not in p]\n"
                    "result = ''.join(filtered)\n"
                    "\n"
                    "if result.strip():\n"
                    "    sys.stdout.write(result)\n"
                    "else:\n"
                    "    sys.exit(1)\n"
                ),
            ),
            File(
                ".",
                "extract_test_targets.py",
                (
                    "import re, sys\n"
                    "\n"
                    "patches = sys.argv[1:]\n"
                    "test_files = set()\n"
                    "test_classes = set()\n"
                    "test_methods = set()\n"
                    "for patch_path in patches:\n"
                    "    try:\n"
                    "        content = open(patch_path).read()\n"
                    "    except (IOError, OSError):\n"
                    "        continue\n"
                    "    for m in re.finditer(r'^diff --git a/(\\S+) b/(\\S+)', content, re.MULTILINE):\n"
                    "        path = m.group(2)\n"
                    "        if path.endswith('_test.py') or '/test_' in path or '/tests/' in path:\n"
                    "            test_files.add(path)\n"
                    "    for m in re.finditer(r'^\\+class (\\w+Test\\w*|Test\\w+)\\(', content, re.MULTILINE):\n"
                    "        test_classes.add(m.group(1))\n"
                    "    for m in re.finditer(r'^\\+    def (test_\\w+)\\(', content, re.MULTILINE):\n"
                    "        test_methods.add(m.group(1))\n"
                    "print(' '.join(sorted(test_files)) if test_files else '')\n"
                    "names = sorted(test_classes | test_methods)\n"
                    "print(' or '.join(names) if names else '')\n"
                ),
            ),
            File(
                ".",
                "run.sh",
                (
                    "#!/bin/bash\n"
                    "set -eo pipefail\n"
                    "cd /home/{repo}\n"
                    "git checkout -- . 2>/dev/null || true\n"
                    "export KERAS_BACKEND=numpy\n"
                    "TARGETS=$(python3 /home/extract_test_targets.py /home/test.patch /home/fix.patch)\n"
                    "TEST_FILES=$(echo \"$TARGETS\" | sed -n '1p')\n"
                    "EXISTING_FILES=\"\"\n"
                    "if [ -n \"$TEST_FILES\" ]; then\n"
                    "    for f in $TEST_FILES; do\n"
                    "        [ -f \"$f\" ] && EXISTING_FILES=\"$EXISTING_FILES $f\"\n"
                    "    done\n"
                    "    EXISTING_FILES=$(echo \"$EXISTING_FILES\" | xargs)\n"
                    "fi\n"
                    "if [ -n \"$EXISTING_FILES\" ]; then\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v --timeout=600 $EXISTING_FILES || true\n"
                    "else\n"
                    "    FALLBACK_DIRS=\"\"\n"
                    "    for f in $TEST_FILES; do\n"
                    "        d=$(dirname \"$f\")\n"
                    "        [ -d \"$d\" ] && FALLBACK_DIRS=\"$FALLBACK_DIRS $d\"\n"
                    "    done\n"
                    "    FALLBACK_DIRS=$(echo \"$FALLBACK_DIRS\" | xargs -n1 | sort -u | xargs)\n"
                    "    if [ -n \"$FALLBACK_DIRS\" ]; then\n"
                    "        pytest --no-header -rA --tb=short -p no:cacheprovider -v --timeout=600 $FALLBACK_DIRS || true\n"
                    "    else\n"
                    "        echo '============================= test session starts =============================='\n"
                    "        echo 'No test files or directories exist at base commit.'\n"
                    "        echo '============================== no tests ran in 0.00s ============================='\n"
                    "    fi\n"
                    "fi\n"
                ).format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                (
                    "#!/bin/bash\n"
                    "set -eo pipefail\n"
                    "cd /home/{repo}\n"
                    "git checkout -- . 2>/dev/null || true\n"
                    "if [ -s /home/test.patch ]; then\n"
                    "    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \\\n"
                    "        git apply --whitespace=nowarn /tmp/filtered_test.patch || true\n"
                    "fi\n"
                    "export KERAS_BACKEND=numpy\n"
                    "TARGETS=$(python3 /home/extract_test_targets.py /home/test.patch)\n"
                    "TEST_FILES=$(echo \"$TARGETS\" | sed -n '1p')\n"
                    "K_EXPR=$(echo \"$TARGETS\" | sed -n '2p')\n"
                    "if [ -n \"$TEST_FILES\" ] && [ -n \"$K_EXPR\" ]; then\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v $TEST_FILES -k \"$K_EXPR\"\n"
                    "elif [ -n \"$TEST_FILES\" ]; then\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v $TEST_FILES\n"
                    "else\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v\n"
                    "fi\n"
                ).format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                (
                    "#!/bin/bash\n"
                    "set -eo pipefail\n"
                    "cd /home/{repo}\n"
                    "git checkout -- . 2>/dev/null || true\n"
                    "if [ -s /home/test.patch ]; then\n"
                    "    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch 2>/dev/null && \\\n"
                    "        git apply --whitespace=nowarn /tmp/filtered_test.patch || true\n"
                    "fi\n"
                    "if [ -s /home/fix.patch ]; then\n"
                    "    python3 /home/filter_patch.py /home/fix.patch > /tmp/filtered_fix.patch 2>/dev/null && \\\n"
                    "        git apply --whitespace=nowarn /tmp/filtered_fix.patch || true\n"
                    "fi\n"
                    "export KERAS_BACKEND=numpy\n"
                    "TARGETS=$(python3 /home/extract_test_targets.py /home/test.patch /home/fix.patch)\n"
                    "TEST_FILES=$(echo \"$TARGETS\" | sed -n '1p')\n"
                    "K_EXPR=$(echo \"$TARGETS\" | sed -n '2p')\n"
                    "if [ -n \"$TEST_FILES\" ] && [ -n \"$K_EXPR\" ]; then\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v $TEST_FILES -k \"$K_EXPR\"\n"
                    "elif [ -n \"$TEST_FILES\" ]; then\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v $TEST_FILES\n"
                    "else\n"
                    "    pytest --no-header -rA --tb=short -p no:cacheprovider -v\n"
                    "fi\n"
                ).format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    build-essential \\
    curl \\
    gnupg \\
    pkg-config \\
    libhdf5-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy pytest pytest-timeout jax[cpu] scipy

WORKDIR /home/

RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {base_sha}

RUN pip install --no-cache-dir .

{copy_commands}""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


@Instance.register("keras-team", "keras")
class Keras(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log_clean = re.sub(r"\x1b\[.*?m", "", log)

        pattern = re.compile(
            r"([\w/.-]+\.py::[\w\[\]._-]+(?:::[\w\[\]._-]+)*)\s+(PASSED|FAILED|SKIPPED|ERROR)|"
            r"(PASSED|FAILED|SKIPPED|ERROR)\s+([\w/.-]+\.py::[\w\[\]._-]+(?:::[\w\[\]._-]+)*)"
        )

        for match in pattern.finditer(log_clean):
            if match.group(1):
                test_name = match.group(1).strip()
                status = match.group(2)
            elif match.group(3):
                status = match.group(3)
                test_name = match.group(4).strip()
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
