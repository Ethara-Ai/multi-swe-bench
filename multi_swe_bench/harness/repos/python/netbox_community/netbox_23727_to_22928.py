from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)

_TEST_HEAD_RE = re.compile(r"^(?P<method>test\w*) \((?P<path>[\w.]+)\)")
_STATUS_RE = re.compile(
    r"\.\.\. (?P<status>ok|FAIL|ERROR|skipped|expected failure|unexpected success)\b"
)
_SUMMARY_RE = re.compile(r"^(?:FAIL|ERROR): (?P<method>test\w*) \((?P<path>[\w.]+)\)")


def _test_labels(test_patch: str) -> list[str]:
    labels = []
    for path in _DIFF_HEADER_RE.findall(test_patch):
        name = path.rsplit("/", 1)[-1]
        if not (path.startswith("netbox/") and "/tests/" in path):
            continue
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        labels.append(path)
    return labels


def _test_command(test_patch: str) -> str:
    modules = " ".join(shlex.quote(path) for path in _test_labels(test_patch))
    return f"""labels=()
for module in {modules}; do
    if [ -f "$module" ]; then
        label="${{module#netbox/}}"
        label="${{label%.py}}"
        labels+=("${{label//\\//.}}")
    fi
done
if [ ${{#labels[@]}} -eq 0 ]; then
    echo "no test modules to run"
    exit 0
fi
cd netbox
python manage.py test "${{labels[@]}}" -v 2 --parallel --noinput
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_START_SERVICES_SH = """#!/bin/bash
set -e

service postgresql start
redis-server --daemonize yes
until pg_isready -q; do sleep 1; done
until redis-cli ping > /dev/null 2>&1; do sleep 1; done
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

python -m venv /home/venv
/home/venv/bin/pip install --no-cache-dir --upgrade pip
/home/venv/bin/pip install --no-cache-dir -r requirements.txt tblib

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if ! git diff --quiet -- requirements.txt; then
    /home/venv/bin/pip download --no-cache-dir -d /home/wheels -r requirements.txt
fi
git reset --hard
git clean -fdx

export NETBOX_CONFIGURATION=netbox.configuration_testing
bash /home/start_services.sh
/home/venv/bin/python netbox/manage.py collectstatic --no-input > /dev/null
service postgresql stop
redis-cli shutdown nosave || true

bash /home/check_git_changes.sh
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export NETBOX_CONFIGURATION=netbox.configuration_testing
export PATH="/home/venv/bin:$PATH"

bash /home/start_services.sh

cd /home/__REPO__
"""


class Netbox23727To22928ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "python:3.12-slim-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {self.pr.number_interval} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential libpq-dev libffi-dev libssl-dev libxml2-dev libxslt1-dev \\
        zlib1g-dev libjpeg-dev postgresql postgresql-contrib redis-server \\
    && rm -rf /var/lib/apt/lists/*

RUN service postgresql start \\
    && su postgres -c "psql -c \\"CREATE USER netbox WITH PASSWORD 'netbox' CREATEDB SUPERUSER;\\"" \\
    && su postgres -c "createdb -O netbox netbox" \\
    && service postgresql stop

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Netbox23727To22928ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return Netbox23727To22928ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return template.replace("__REPO__", self.pr.repo).replace(
            "__BASE_SHA__", self.pr.base.sha
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        test_cmd = _test_command(self.pr.test_patch)
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = """git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -d /home/wheels ]; then
    pip install --no-index --find-links /home/wheels -r requirements.txt
fi"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "start_services.sh", _START_SERVICES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{test_cmd}"),
            File(".", "test-run.sh", f"{header}\n{test_apply}\n\n{test_cmd}"),
            File(".", "fix-run.sh", f"{header}\n{fix_apply}\n\n{test_cmd}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        hardening = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', self.pr.base.sha)

        return f"""FROM {image.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def name_of(method: str, path: str) -> str:
        return path if path.endswith("." + method) else f"{path}.{method}"

    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    pending = None
    for line in test_log.splitlines():
        line = line.strip()
        head = _TEST_HEAD_RE.match(line)
        if head:
            pending = name_of(head.group("method"), head.group("path"))
        status = _STATUS_RE.search(line)
        if status and pending:
            value = status.group("status")
            if value in ("ok", "unexpected success"):
                passed_tests.add(pending)
            elif value in ("FAIL", "ERROR"):
                failed_tests.add(pending)
            else:
                skipped_tests.add(pending)
            pending = None
            continue
        summary = _SUMMARY_RE.match(line)
        if summary:
            failed_tests.add(name_of(summary.group("method"), summary.group("path")))

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


@Instance.register("netbox-community", "netbox_23727_to_22928")
class NETBOX_23727_TO_22928(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Netbox23727To22928ImageDefault(self.pr, self._config)

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
        return _parse_log(test_log)
