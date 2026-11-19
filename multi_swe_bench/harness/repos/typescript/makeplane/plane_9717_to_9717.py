import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



_SERVICES = """start_services() {
  service postgresql start >/dev/null 2>&1 || true
  redis-server --daemonize yes >/dev/null 2>&1 || true
  for _ in $(seq 1 60); do
    if pg_isready -q; then break; fi
    sleep 1
  done
  pg_isready
}
start_services"""

_TEST_ENV = """export CI=true
export POSTGRES_DB=plane POSTGRES_USER=postgres POSTGRES_PASSWORD=plane
export POSTGRES_HOST=localhost POSTGRES_PORT=5432
export SECRET_KEY=dummysecret DJANGO_SETTINGS_MODULE=plane.settings.test
export REDIS_URL=redis://localhost:6379/ REDIS_HOST=localhost REDIS_PORT=6379
export AMQP_URL=redis://localhost:6379/1
export APP_BASE_URL=http://localhost WEB_URL=http://localhost
export PYTHONDONTWRITEBYTECODE=1
export PATH=/venv/bin:$PATH"""

_PYTEST_TARGETS = "plane/tests/unit/serializers plane/tests/contract/api"
_TEST_CMD = (
    f"python -m pytest {_PYTEST_TARGETS} "
    "-p no:cacheprovider --continue-on-collection-errors -rA"
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""


class PlaneImageBase(Image):

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
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return [
            "python3-venv",
            "python3-dev",
            "libpq-dev",
            "postgresql",
            "postgresql-contrib",
            "redis-server",
        ]

    def extra_setup(self) -> str:
        return """RUN python3 -m venv /venv && \\
    /venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel && \\
    /venv/bin/pip install --no-cache-dir -r /home/plane/apps/api/requirements/test.txt

RUN set -eux; \\
    service postgresql start; \\
    for _ in $(seq 1 60); do pg_isready -q && break; sleep 1; done; \\
    pg_isready; \\
    su postgres -c "psql -c \\"ALTER USER postgres PASSWORD 'plane';\\""; \\
    su postgres -c "createdb plane"; \\
    cd /home/plane/apps/api; \\
    export POSTGRES_DB=plane POSTGRES_USER=postgres POSTGRES_PASSWORD=plane; \\
    export POSTGRES_HOST=localhost POSTGRES_PORT=5432; \\
    export SECRET_KEY=dummysecret DJANGO_SETTINGS_MODULE=plane.settings.test; \\
    export REDIS_URL=redis://localhost:6379/ REDIS_HOST=localhost REDIS_PORT=6379; \\
    export AMQP_URL=redis://localhost:6379/1; \\
    export APP_BASE_URL=http://localhost WEB_URL=http://localhost; \\
    /venv/bin/python -c "import django; django.setup(); print('DJANGO_OK')"; \\
    /venv/bin/python -m pytest plane/tests/unit/serializers plane/tests/contract/api \\
        --collect-only -p no:cacheprovider > /tmp/collect.txt 2>&1; \\
    tail -3 /tmp/collect.txt; \\
    COLLECTED=$(sed -nE 's/.*[^0-9]([0-9]+) tests? collected.*/\\1/p' /tmp/collect.txt | tail -1); \\
    echo "collected=${COLLECTED:-0}"; \\
    test "${COLLECTED:-0}" -gt 0; \\
    rm -f /tmp/collect.txt; \\
    echo DEPS_OK"""


class PlaneImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return PlaneImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh


{services}

export POSTGRES_DB=plane POSTGRES_USER=postgres POSTGRES_PASSWORD=plane
export POSTGRES_HOST=localhost POSTGRES_PORT=5432
export SECRET_KEY=dummysecret DJANGO_SETTINGS_MODULE=plane.settings.test
export REDIS_URL=redis://localhost:6379/ REDIS_HOST=localhost REDIS_PORT=6379
export AMQP_URL=redis://localhost:6379/1
export APP_BASE_URL=http://localhost WEB_URL=http://localhost
export PATH=/venv/bin:$PATH

cd /home/{pr.repo}/apps/api
python -c "import django; django.setup(); print('DJANGO_OK')"

python -m pytest {targets} --collect-only -p no:cacheprovider > /tmp/collect.txt 2>&1
tail -3 /tmp/collect.txt
COLLECTED=$(sed -nE 's/.*[^0-9]([0-9]+) tests? collected.*/\\1/p' /tmp/collect.txt | tail -1)
echo "collected=${{COLLECTED:-0}}"
test "${{COLLECTED:-0}}" -gt 0
rm -f /tmp/collect.txt
echo DEPS_OK
""".format(pr=self.pr, services=_SERVICES, targets=_PYTEST_TARGETS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

{services}

cd /home/{pr.repo}/apps/api
{cmd}
""".format(pr=self.pr, env=_TEST_ENV, services=_SERVICES, cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

{services}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

cd /home/{pr.repo}/apps/api
{cmd}
""".format(pr=self.pr, env=_TEST_ENV, services=_SERVICES, cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

{services}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

cd /home/{pr.repo}/apps/api
{cmd}
""".format(pr=self.pr, env=_TEST_ENV, services=_SERVICES, cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
WORKDIR /home

CMD ["/bin/bash"]
"""


@Instance.register("makeplane", "plane_9717_to_9717")
class PLANE_9717_TO_9717(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PlaneImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        node = r"[^\s:]+\.py::[^\s]*"
        status_kw = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"

        re_inline = re.compile(rf"^({node}(?:\[[^\]]*\])?)\s+({status_kw})\b")
        re_summary = re.compile(rf"^({status_kw})\s+({node}(?:\[[^\]]*\])?)")

        for raw_line in clean_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            name = status = None

            match = re_inline.match(line)
            if match:
                name, status = match.group(1), match.group(2)
            else:
                match = re_summary.match(line)
                if match:
                    status, name = match.group(1), match.group(2)

            if not name or not status:
                continue

            if name.startswith("plane/"):
                name = f"apps/api/{name}"

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


Instance.register("makeplane", "plane")(PLANE_9717_TO_9717)
