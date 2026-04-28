import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# StackStorm/st2 — Era 2: PRs 5038–5605 (2020-09-10 to 2022-04-01)
# Python event-driven automation platform
# Base commits use mongoengine==0.23.0, cryptography==3.4.7, eventlet==0.30.2
# Tests via nosetests with rednose (NOT pytest)
# Requires MongoDB 4.4 + RabbitMQ at runtime (installed in prepare.sh)
# st2debug removed, mistral_v2 runner removed vs Era 1
REPO_DIR = "st2"


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
        return "python:3.8-slim-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        st2_packages = (
            "st2actions st2api st2auth st2client st2common "
            "st2exporter st2reactor st2stream st2tests"
        )
        runners = (
            "contrib/runners/action_chain_runner "
            "contrib/runners/announcement_runner "
            "contrib/runners/http_runner "
            "contrib/runners/inquirer_runner "
            "contrib/runners/local_runner "
            "contrib/runners/noop_runner "
            "contrib/runners/orquesta_runner "
            "contrib/runners/python_runner "
            "contrib/runners/remote_runner "
            "contrib/runners/winrm_runner"
        )
        all_components = st2_packages.split() + runners.split()

        pythonpath_parts = [f"/home/{REPO_DIR}/{c}" for c in all_components]
        pythonpath = ":".join(pythonpath_parts)

        components_test = (
            "st2actions st2api st2auth st2client st2common "
            "st2reactor st2stream st2tests"
        )

        prepare_sh = f"""apt-get update && apt-get install -y --no-install-recommends \\
    build-essential libldap2-dev libsasl2-dev libssl-dev libyaml-dev \\
    ldap-utils curl gnupg wget ca-certificates
###ACTION_DELIMITER###
wget -qO - https://www.mongodb.org/static/pgp/server-4.4.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-4.4.gpg && \\
    echo 'deb [signed-by=/usr/share/keyrings/mongodb-server-4.4.gpg arch=arm64,amd64] http://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse' | tee /etc/apt/sources.list.d/mongodb-org-4.4.list && \\
    apt-get update && apt-get install -y mongodb-org
###ACTION_DELIMITER###
mkdir -p /data/db && mongod --fork --logpath /var/log/mongodb.log
###ACTION_DELIMITER###
apt-get install -y rabbitmq-server && rabbitmq-server -detached && sleep 2 && rabbitmqctl await_startup
###ACTION_DELIMITER###
pip install --upgrade "pip<23.0" setuptools wheel
###ACTION_DELIMITER###
pip install --use-deprecated=legacy-resolver \\
    "pyOpenSSL<=21.0.0" \\
    "MarkupSafe<2.1.0,>=0.23" \\
    -r requirements.txt
###ACTION_DELIMITER###
pip install --use-deprecated=legacy-resolver -r test-requirements.txt
###ACTION_DELIMITER###
for component in {st2_packages}; do
    if [ -d "$component" ] && [ -f "$component/setup.py" ]; then
        cd /home/{REPO_DIR}/$component && python setup.py develop --no-deps 2>/dev/null || true
        cd /home/{REPO_DIR}
    fi
done
###ACTION_DELIMITER###
for runner in {runners}; do
    if [ -d "$runner" ] && [ -f "$runner/setup.py" ]; then
        cd /home/{REPO_DIR}/$runner && python setup.py develop --no-deps 2>/dev/null || true
        cd /home/{REPO_DIR}
    fi
done
###ACTION_DELIMITER###
export PYTHONPATH="{pythonpath}"
###ACTION_DELIMITER###
python -c "import st2tests; print('st2tests import OK')"
"""

        run_sh = f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}

export PYTHONPATH="{pythonpath}"

# Start services if not running
mongod --fork --logpath /var/log/mongodb.log 2>/dev/null || true
rabbitmq-server -detached 2>/dev/null || true
sleep 1

# Drop test database
python -c "
from pymongo import MongoClient
MongoClient('127.0.0.1', 27017).drop_database('st2-test')
print('Dropped st2-test database')
"

# Run unit tests across all components
for component in {components_test}; do
    if [ -d "$component/tests/unit" ]; then
        echo "=== Running tests for $component ==="
        nosetests --rednose --immediate -s -v $component/tests/unit/ || true
    fi
done
"""

        test_run_sh = f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}

export PYTHONPATH="{pythonpath}"

# Apply test patch
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# Start services if not running
mongod --fork --logpath /var/log/mongodb.log 2>/dev/null || true
rabbitmq-server -detached 2>/dev/null || true
sleep 1

# Drop test database
python -c "
from pymongo import MongoClient
MongoClient('127.0.0.1', 27017).drop_database('st2-test')
print('Dropped st2-test database')
"

# Run unit tests across all components
for component in {components_test}; do
    if [ -d "$component/tests/unit" ]; then
        echo "=== Running tests for $component ==="
        nosetests --rednose --immediate -s -v $component/tests/unit/ || true
    fi
done
"""

        fix_run_sh = f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}

export PYTHONPATH="{pythonpath}"

# Apply test and fix patches
if ! git -C /home/{REPO_DIR} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# Start services if not running
mongod --fork --logpath /var/log/mongodb.log 2>/dev/null || true
rabbitmq-server -detached 2>/dev/null || true
sleep 1

# Drop test database
python -c "
from pymongo import MongoClient
MongoClient('127.0.0.1', 27017).drop_database('st2-test')
print('Dropped st2-test database')
"

# Run unit tests across all components
for component in {components_test}; do
    if [ -d "$component/tests/unit" ]; then
        echo "=== Running tests for $component ==="
        nosetests --rednose --immediate -s -v $component/tests/unit/ || true
    fi
done
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = """
FROM python:3.8-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    build-essential \\
    gnupg \\
    wget \\
    libldap2-dev \\
    libsasl2-dev \\
    libssl-dev \\
    libyaml-dev \\
    ldap-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN if [ ! -f /bin/bash ]; then \\
        if command -v apk >/dev/null 2>&1; then \\
            apk add --no-cache bash; \\
        elif command -v apt-get >/dev/null 2>&1; then \\
            apt-get update && apt-get install -y bash; \\
        elif command -v yum >/dev/null 2>&1; then \\
            yum install -y bash; \\
        else \\
            exit 1; \\
        fi \\
    fi

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/StackStorm/st2.git /home/st2

WORKDIR /home/st2
RUN git reset --hard
RUN git checkout {pr.base.sha}

"""
        dockerfile_content += f"""
{copy_commands}
RUN bash /home/prepare.sh
"""
        dockerfile_content += """
CMD ["/bin/bash"]
"""
        return dockerfile_content.format(pr=self.pr)


@Instance.register("StackStorm", "st2_5038_to_5605")
class ST2_5038_TO_5605(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        """Parse nosetests rednose verbose output from st2 unit tests.

        Matches lines like:
            test_method (module.path.TestClass) ... passed
            test_method (module.path.TestClass) ... FAILED
            test_method (module.path.TestClass) ... ERROR
            test_method (module.path.TestClass) ... skipped
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Nosetests rednose output format:
        # test_name (module.path.TestClass) ... STATUS
        # where STATUS is: passed (lowercase), FAILED (uppercase), ERROR (uppercase), skipped (lowercase)
        pattern = re.compile(
            r"^(.*?)\s+\(([^)]+)\)\s+\.\.\.\s+(passed|FAILED|ERROR|skipped)",
            re.MULTILINE,
        )
        for match in pattern.finditer(test_log):
            test_name = match.group(1).strip()
            module_path = match.group(2).strip()
            status = match.group(3)
            full_test_name = f"{module_path}.{test_name}"

            if status == "passed":
                passed_tests.add(full_test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(full_test_name)
            elif status == "skipped":
                skipped_tests.add(full_test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
