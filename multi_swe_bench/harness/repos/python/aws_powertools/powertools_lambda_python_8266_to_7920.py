import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "aws-powertools"
_REPO = "powertools-lambda-python"
_INTERVAL = "powertools_lambda_python_8266_to_7920"

_BOUNDS = [int(n) for n in re.search(r"_(\d+)_to_(\d+)$", _INTERVAL).groups()]
_PR_MIN = min(_BOUNDS)
_PR_MAX = max(_BOUNDS)

_BASE_TAG = f"base-{_PR_MAX}_to_{_PR_MIN}"

_PYTHON_VERSION = "3.12"
_POETRY_VERSION = "2.4.1"

_ROUTING_FLAG = "_powertools_lambda_python_8266_to_7920_routed"

if not getattr(Instance, _ROUTING_FLAG, False):
    _orig_create = Instance.create.__func__

    def _routed_create(cls, pr, config, *args, **kwargs):
        if (
            not pr.number_interval
            and pr.org.lower() == _ORG
            and pr.repo == _REPO
            and _PR_MIN <= pr.number <= _PR_MAX
        ):
            pr.number_interval = _INTERVAL
        return _orig_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_routed_create)
    setattr(Instance, _ROUTING_FLAG, True)


_SHELL_ENV = """\
export CI=true
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export POETRY_VIRTUALENVS_IN_PROJECT=true
export POETRY_VIRTUALENVS_CREATE=true
export POETRY_NO_INTERACTION=1
export POETRY_REQUESTS_TIMEOUT=120
export FINDPYTHON_GET_VERSION_TIMEOUT=120
export AWS_DEFAULT_REGION=us-east-1
export PATH="/root/.local/bin:$PATH\""""

_PREPARE_CMD = f"""\
test -f Makefile
test -f poetry.lock
EXTRAS="$(grep -m1 -oE 'poetry install --extras "[^"]*"' Makefile | sed -E 's/.*"([^"]*)"/\\1/')"
test -n "$EXTRAS"

rm -rf .venv
poetry env use "$(command -v python{_PYTHON_VERSION})"
for attempt in 1 2 3; do
    poetry install --no-interaction --extras "$EXTRAS" && break
    [ "$attempt" -lt 3 ] || {{ echo "prepare.sh: poetry install failed 3 times"; exit 1; }}
    sleep 30
done
rm -rf /root/.cache/pypoetry /root/.cache/pip

.venv/bin/python --version 2>&1 | grep -q "Python {_PYTHON_VERSION}\\."
.venv/bin/python -c "import aws_lambda_powertools, pytest, pytest_asyncio, pytest_mock"
.venv/bin/python -m pytest --version
echo DEPS_OK"""

_TEST_CMD = """\
TESTS=()
while read -r f; do
    b="$(basename "$f")"
    case "$b" in
        test_*.py|*_test.py) ;;
        *) continue ;;
    esac
    [ -f "$f" ] || continue
    TESTS+=("$f")
done < <(grep -E '^diff --git a/' /home/test.patch | awk '{print $3; print $4}' | sed -E 's#^[ab]/##' | sort -u)

if [ "${#TESTS[@]}" -eq 0 ]; then
    echo "No test files from test.patch present"
    exit 0
fi

.venv/bin/python -m pytest \\
    -p no:cacheprovider \\
    -rA \\
    -vv \\
    --tb=short \\
    --color=no \\
    "${TESTS[@]}\""""

_CHECK_GIT_CHANGES = """\
#!/bin/bash
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

_PREPARE_TEMPLATE = """\
#!/bin/bash
set -euo pipefail
{shell_env}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{prepare_cmd}
"""

_RUN_TEMPLATE = """\
#!/bin/bash
set -eo pipefail
{shell_env}

cd /home/{repo}
{apply}{test_cmd}
"""


class PowertoolsLambdaPython8266To7920ImageBase(Image):
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
        return f"python:{_PYTHON_VERSION}-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{DockerfileEnhancer._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl make build-essential pkg-config libssl-dev libffi-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir "poetry=={_POETRY_VERSION}" && \\
    poetry --version | grep -q "{_POETRY_VERSION}"

RUN git config --system --add safe.directory '*' && \\
    git config --global --add safe.directory '*'

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class PowertoolsLambdaPython8266To7920ImageDefault(Image):
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
        return PowertoolsLambdaPython8266To7920ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        def run_script(apply: str) -> str:
            return _RUN_TEMPLATE.format(
                shell_env=_SHELL_ENV,
                repo=repo,
                apply=apply,
                test_cmd=_TEST_CMD,
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                _PREPARE_TEMPLATE.format(
                    shell_env=_SHELL_ENV,
                    repo=repo,
                    sha=self.pr.base.sha,
                    prepare_cmd=_PREPARE_CMD,
                ),
            ),
            File(".", "run.sh", run_script("")),
            File(
                ".",
                "test-run.sh",
                run_script("git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                run_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

"""


@Instance.register(_ORG, _INTERVAL)
class POWERTOOLS_LAMBDA_PYTHON_8266_TO_7920(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PowertoolsLambdaPython8266To7920ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        verbose_re = re.compile(
            r"^(\S+\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?: \(.*?\))?(?:\s+\[\s*\d+%\])?$"
        )
        summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS) (\S+\.py::[^\s\[]+(?:\[[^\]]*\])?)(?: - .*)?$"
        )

        def record(test: str, status: str) -> None:
            if status in ("PASSED", "XFAIL"):
                passed_tests.add(test)
            elif status in ("FAILED", "ERROR", "XPASS"):
                failed_tests.add(test)
            elif status == "SKIPPED":
                skipped_tests.add(test)

        for raw in log.splitlines():
            line = raw.strip()
            m = verbose_re.match(line)
            if m:
                record(m.group(1), m.group(2))
                continue
            m = summary_re.match(line)
            if m:
                record(m.group(2), m.group(1))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
