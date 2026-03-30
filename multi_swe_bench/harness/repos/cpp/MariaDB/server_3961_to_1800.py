import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Era3ImageBase(Image):
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
        return "era3-base"

    def workdir(self) -> str:
        return "era3-base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential cmake git \\
    patch pkg-config tar wget curl \\
    libncurses5-dev zlib1g-dev libssl-dev \\
    libreadline-dev bison gnutls-dev libaio-dev \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}
"""


class Era3ImageDefault(Image):
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
        return Era3ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_suites(self) -> str:
        suites = set()
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("diff --git"):
                parts = line.split(" b/")
                if len(parts) > 1:
                    path = parts[1]
                    if "mysql-test/suite/" in path:
                        suite = path.split("mysql-test/suite/")[1].split("/")[0]
                        suites.add(suite)
                    elif "mysql-test/t/" in path or "mysql-test/r/" in path:
                        suites.add("main")
        return ",".join(sorted(suites)) if suites else "main"

    def files(self) -> list[File]:
        suites = self._get_suites()
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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

mkdir -p build && cd build
cmake /home/{pr.repo} \\
    -DWITH_UNIT_TESTS=OFF \\
    -DPLUGIN_TOKUDB=NO \\
    -DPLUGIN_MROONGA=NO \\
    -DPLUGIN_SPIDER=NO \\
    -DPLUGIN_OQGRAPH=NO \\
    -DPLUGIN_PERFSCHEMA=NO \\
    -DPLUGIN_SPHINX=NO
make -j $(nproc)

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

SUITE_DIR="/home/{pr.repo}/build/mysql-test/suite"
MYSQL_TEST_DIR="/home/{pr.repo}/mysql-test/suite"
REQUESTED="{suites}"
VALID=""
IFS=',' read -ra SUITES <<< "$REQUESTED"
for s in "${{SUITES[@]}}"; do
    s=$(echo "$s" | xargs)
    if [ "$s" = "main" ] || [ -d "$SUITE_DIR/$s" ] || [ -d "$MYSQL_TEST_DIR/$s" ]; then
        [ -n "$VALID" ] && VALID="$VALID,$s" || VALID="$s"
    else
        echo "Skipping non-existent suite: $s"
    fi
done
if [ -z "$VALID" ]; then
    echo "ERROR: No valid suites found"
    exit 1
fi

cd /home/{pr.repo}/build/mysql-test
perl mariadb-test-run.pl --suite=$VALID --force --max-test-fail=0 --parallel=2

""".format(pr=self.pr, suites=suites),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

apply_patch() {{
    local patch_file="$1"
    if [ ! -s "$patch_file" ]; then
        echo "Skipping empty patch: $patch_file"
        return 0
    fi
    echo "Applying $patch_file..."
    if git apply --whitespace=nowarn "$patch_file" 2>&1; then
        echo "Applied $patch_file successfully with git apply"
    else
        echo "git apply failed, retrying with patch --fuzz=3..."
        git checkout -- . 2>/dev/null || true
        patch --batch --fuzz=3 -p1 < "$patch_file" 2>&1 || {{
            echo "WARNING: patch partially failed for $patch_file, continuing..."
        }}
    fi
}}

cd /home/{pr.repo}
apply_patch /home/test.patch
git submodule update --init --recursive 2>/dev/null || true

SUITE_DIR="/home/{pr.repo}/build/mysql-test/suite"
MYSQL_TEST_DIR="/home/{pr.repo}/mysql-test/suite"
REQUESTED="{suites}"
VALID=""
IFS=',' read -ra SUITES <<< "$REQUESTED"
for s in "${{SUITES[@]}}"; do
    s=$(echo "$s" | xargs)
    if [ "$s" = "main" ] || [ -d "$SUITE_DIR/$s" ] || [ -d "$MYSQL_TEST_DIR/$s" ]; then
        [ -n "$VALID" ] && VALID="$VALID,$s" || VALID="$s"
    else
        echo "Skipping non-existent suite: $s"
    fi
done
if [ -z "$VALID" ]; then
    echo "ERROR: No valid suites found"
    exit 1
fi

cd build
make -j $(nproc)
cd mysql-test
perl mariadb-test-run.pl --suite=$VALID --force --max-test-fail=0 --parallel=2

""".format(pr=self.pr, suites=suites),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

apply_patch() {{
    local patch_file="$1"
    if [ ! -s "$patch_file" ]; then
        echo "Skipping empty patch: $patch_file"
        return 0
    fi
    echo "Applying $patch_file..."
    if git apply --whitespace=nowarn "$patch_file" 2>&1; then
        echo "Applied $patch_file successfully with git apply"
    else
        echo "git apply failed, retrying with patch --fuzz=3..."
        git checkout -- . 2>/dev/null || true
        patch --batch --fuzz=3 -p1 < "$patch_file" 2>&1 || {{
            echo "WARNING: patch partially failed for $patch_file, continuing..."
        }}
    fi
}}

cd /home/{pr.repo}
apply_patch /home/test.patch
apply_patch /home/fix.patch
git submodule update --init --recursive 2>/dev/null || true

SUITE_DIR="/home/{pr.repo}/build/mysql-test/suite"
MYSQL_TEST_DIR="/home/{pr.repo}/mysql-test/suite"
REQUESTED="{suites}"
VALID=""
IFS=',' read -ra SUITES <<< "$REQUESTED"
for s in "${{SUITES[@]}}"; do
    s=$(echo "$s" | xargs)
    if [ "$s" = "main" ] || [ -d "$SUITE_DIR/$s" ] || [ -d "$MYSQL_TEST_DIR/$s" ]; then
        [ -n "$VALID" ] && VALID="$VALID,$s" || VALID="$s"
    else
        echo "Skipping non-existent suite: $s"
    fi
done
if [ -z "$VALID" ]; then
    echo "ERROR: No valid suites found"
    exit 1
fi

cd build
make -j $(nproc)
cd mysql-test
perl mariadb-test-run.pl --suite=$VALID --force --max-test-fail=0 --parallel=2

""".format(pr=self.pr, suites=suites),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("MariaDB", "server_3961_to_1800")
class Server3961To1800(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Era3ImageDefault(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^([\w\.\-]+(?:\s+'[^']*')?)\s+(?:w\d+\s+)?\[\s*pass\s*\]"),
        ]
        re_fail_tests = [
            re.compile(r"^([\w\.\-]+(?:\s+'[^']*')?)\s+(?:w\d+\s+)?\[\s*fail\s*\]"),
        ]
        re_skip_tests = [
            re.compile(r"^([\w\.\-]+(?:\s+'[^']*')?)\s+(?:w\d+\s+)?\[\s*skipped\s*\]"),
        ]

        for line in clean_log.splitlines():
            line = line.strip()
            if not line:
                continue

            for re_pass in re_pass_tests:
                pass_match = re_pass.match(line)
                if pass_match:
                    test = pass_match.group(1).strip()
                    passed_tests.add(test)

            for re_fail in re_fail_tests:
                fail_match = re_fail.match(line)
                if fail_match:
                    test = fail_match.group(1).strip()
                    failed_tests.add(test)

            for re_skip in re_skip_tests:
                skip_match = re_skip.match(line)
                if skip_match:
                    test = skip_match.group(1).strip()
                    skipped_tests.add(test)

        # Dedup: if a test appears in both passed and failed, count as failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
