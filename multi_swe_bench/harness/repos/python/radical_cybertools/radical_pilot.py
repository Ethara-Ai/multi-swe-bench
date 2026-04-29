import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# PRs that use the OLD radical.utils get_version() 6-value unpack format.
# These need a sed patch to convert to the newer 5-value unpack before install.
OLD_FORMAT_PRS = {2780, 3019, 3163}

# Per-PR test file mapping: only .py test files in tests/unit_tests/ or
# tests/component_tests/ that are touched by the test_patch.
RADICAL_PILOT_PR_TESTS = {
    2780: [
        "tests/unit_tests/test_raptor/test_master.py",
    ],
    3117: [
        "tests/component_tests/test_stager.py",
        "tests/unit_tests/test_agent_0/test_agent_0.py",
        "tests/unit_tests/test_executing/test_popen.py",
        "tests/unit_tests/test_lm/test_fork.py",
        "tests/unit_tests/test_lm/test_ibrun.py",
        "tests/unit_tests/test_lm/test_jsrun.py",
        "tests/unit_tests/test_lm/test_mpiexec.py",
        "tests/unit_tests/test_lm/test_prte.py",
        "tests/unit_tests/test_lm/test_rsh.py",
        "tests/unit_tests/test_lm/test_srun.py",
        "tests/unit_tests/test_lm/test_ssh.py",
        "tests/unit_tests/test_pilot/test_pilot.py",
        "tests/unit_tests/test_rm/test_cobalt.py",
        "tests/unit_tests/test_rm/test_lsf.py",
        "tests/unit_tests/test_rm/test_pbspro.py",
        "tests/unit_tests/test_rm/test_slurm.py",
        "tests/unit_tests/test_rm/test_torque.py",
        "tests/unit_tests/test_scheduler/test_base.py",
        "tests/unit_tests/test_scheduler/test_continuous.py",
    ],
    3163: [
        "tests/unit_tests/test_agent_stagein/test_default.py",
        "tests/unit_tests/test_scheduler/test_base.py",
    ],
    3167: [
        "tests/component_tests/test_stager.py",
    ],
    3199: [
        "tests/unit_tests/test_rm/test_base.py",
    ],
    3208: [
        "tests/unit_tests/test_agent_0/test_agent_0.py",
        "tests/unit_tests/test_executing/test_popen.py",
        "tests/unit_tests/test_scheduler/test_continuous.py",
    ],
    3318: [
        "tests/unit_tests/test_launcher/test_launcher.py",
        "tests/unit_tests/test_lm/test_aprun.py",
        "tests/unit_tests/test_lm/test_base.py",
        "tests/unit_tests/test_lm/test_ccmrun.py",
        "tests/unit_tests/test_lm/test_fork.py",
        "tests/unit_tests/test_lm/test_ibrun.py",
        "tests/unit_tests/test_lm/test_jsrun.py",
        "tests/unit_tests/test_lm/test_mpiexec.py",
        "tests/unit_tests/test_lm/test_mpirun.py",
        "tests/unit_tests/test_lm/test_prte.py",
        "tests/unit_tests/test_lm/test_rsh.py",
        "tests/unit_tests/test_lm/test_srun.py",
        "tests/unit_tests/test_lm/test_ssh.py",
        "tests/unit_tests/test_rm/test_base.py",
        "tests/unit_tests/test_rm/test_cobalt.py",
        "tests/unit_tests/test_rm/test_fork.py",
        "tests/unit_tests/test_rm/test_lsf.py",
        "tests/unit_tests/test_rm/test_pbspro.py",
        "tests/unit_tests/test_rm/test_slurm.py",
        "tests/unit_tests/test_rm/test_torque.py",
        "tests/unit_tests/test_scheduler/test_base.py",
        "tests/unit_tests/test_scheduler/test_continuous.py",
    ],
    # PR#3019: test_patch only touches docs (testing_guidelines.rst) - no test files
    # PR#3270: test_patch only touches JSON test case data files - no test .py files
}


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
        return "python:3.9-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        base_sha = self.pr.base.sha
        test_files = RADICAL_PILOT_PR_TESTS.get(self.pr.number, [])
        test_files_str = " ".join(test_files)
        is_old_format = self.pr.number in OLD_FORMAT_PRS

        # Build the version patch commands for old-format PRs.
        # Old PRs unpack 6 values from radical.utils.get_version() but
        # radical.utils@devel returns only 5. We patch __init__.py to match.
        version_patch_cmds = ""
        if is_old_format:
            version_patch_cmds = r"""
# Patch old-format __init__.py: 6-value unpack -> 5-value unpack
# radical.utils@devel get_version() returns (short, base, branch, tag, detail)
sed -i 's/version_short, version_detail, version_base, version_branch, \\/version_short, version_base, version_branch, \\/' src/radical/pilot/__init__.py
sed -i 's/        sdist_name, sdist_path = _ru.get_version/        version_tag, version_detail = _ru.get_version/' src/radical/pilot/__init__.py
"""

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
                "prepare.sh",
                f"""#!/bin/bash
set -e
cd /home/{repo_name}

# 1. Checkout base commit
git checkout {base_sha}
{version_patch_cmds}
# 2. Write VERSION file (required by setup.py, namespace package)
mkdir -p src/radical/pilot
cat > src/radical/pilot/VERSION << 'VEOF'
1.0.0
1.0.0
devel
v1.0.0
1.0.0-v1.0.0@devel
VEOF

# 3. Install dependencies from devel branches + package itself
pip install --upgrade pip
pip install "setuptools<70"
pip install git+https://github.com/radical-cybertools/radical.utils.git@devel
pip install git+https://github.com/radical-cybertools/radical.gtod.git@devel
pip install git+https://github.com/radical-cybertools/radical.saga.git@devel
pip install setproctitle dill psij-python requests cffi "pymongo<4"
pip install -e . --no-build-isolation --no-deps
pip install pytest pytest-timeout

# 4. Re-create VERSION file (setup.py cleanup removes it during install)
cat > src/radical/pilot/VERSION << 'VEOF'
1.0.0
1.0.0
devel
v1.0.0
1.0.0-v1.0.0@devel
VEOF
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    EXISTING_TESTS="tests/unit_tests/ tests/component_tests/"
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors 2>&1
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    EXISTING_TESTS="tests/unit_tests/ tests/component_tests/"
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors 2>&1
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
cd /home/{repo_name}

if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/fix.patch; then
    echo "Error: git apply fix.patch failed" >&2
    exit 1
fi
if ! git -C /home/{repo_name} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

EXISTING_TESTS=""
for f in {test_files_str}; do
    if [ -f "$f" ]; then
        EXISTING_TESTS="$EXISTING_TESTS $f"
    fi
done
if [ -z "$EXISTING_TESTS" ]; then
    EXISTING_TESTS="tests/unit_tests/ tests/component_tests/"
fi
python -m pytest $EXISTING_TESTS --no-header -rA --tb=no -p no:cacheprovider -v --continue-on-collection-errors 2>&1
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = f"""
FROM python:3.9-bookworm

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git sudo curl && \\
    rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh
"""
        return dockerfile_content


@Instance.register("radical-cybertools", "radical.pilot")
class RADICAL_PILOT(Instance):
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

        for line in log.split("\n"):
            line = line.strip()
            if line.startswith("PASSED "):
                test_name = line[len("PASSED "):].strip()
                passed_tests.add(test_name)
            elif line.startswith("FAILED "):
                test_name = line[len("FAILED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                failed_tests.add(test_name)
            elif line.startswith("ERROR "):
                test_name = line[len("ERROR "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                failed_tests.add(test_name)
            elif line.startswith("SKIPPED "):
                test_name = line[len("SKIPPED "):].strip()
                if " - " in test_name:
                    test_name = test_name.split(" - ")[0]
                skipped_tests.add(test_name)
            else:
                match = re.match(
                    r"^(.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL)\s*(\[.*\])?$", line
                )
                if match:
                    test_name = match.group(1)
                    status = match.group(2)
                    if status == "PASSED":
                        passed_tests.add(test_name)
                    elif status in ("FAILED", "ERROR"):
                        failed_tests.add(test_name)
                    elif status == "SKIPPED":
                        skipped_tests.add(test_name)
                    elif status == "XFAIL":
                        passed_tests.add(test_name)

        # Conflict resolution: if test in both passed and failed, keep failed
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
