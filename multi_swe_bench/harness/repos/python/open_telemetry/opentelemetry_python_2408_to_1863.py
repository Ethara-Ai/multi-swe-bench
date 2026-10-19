from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.8-bookworm"
_BASE_TAG = "base-2408_to_1863"

_PIP_TOOLING = '"pip==23.0.1" "setuptools==59.6.0" "wheel==0.37.1"'
_PIP_RUNTIME = (
    '"Deprecated==1.2.13" "wrapt==1.14.1" "typing-extensions==4.1.1" '
    '"six==1.16.0" "protobuf==3.20.3" "grpcio==1.48.2" '
    '"googleapis-common-protos==1.56.4" "backoff==1.11.1" "thrift==0.16.0" '
    '"py-cpuinfo==8.0.0" "pytest==7.1.3" "pytest-benchmark==3.4.1"'
)

_PACKAGE_DIRS = """opentelemetry-api
opentelemetry-semantic-conventions
opentelemetry-sdk
opentelemetry-proto
tests/util
tests/opentelemetry-test-utils
exporter/opentelemetry-exporter-jaeger-proto-grpc
exporter/opentelemetry-exporter-jaeger-thrift
exporter/opentelemetry-exporter-otlp-proto-grpc"""

_TEST_DIRS = """opentelemetry-api/tests
opentelemetry-semantic-conventions/tests
opentelemetry-sdk/tests
opentelemetry-proto/tests
exporter/opentelemetry-exporter-jaeger-proto-grpc/tests
exporter/opentelemetry-exporter-jaeger-thrift/tests
exporter/opentelemetry-exporter-otlp-proto-grpc/tests"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_ID = r"[^\s:]+\.py::.+?"
_VERBOSE_RE = re.compile(
    rf"^({_TEST_ID})(?:\s+<-\s+\S+)?\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\(.*\))?(?:\s+\[\s*\d+%\])?$"
)
_SUMMARY_RE = re.compile(
    rf"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+({_TEST_ID})(?:\s+-\s.*)?$"
)
_COLLECTION_ERROR_RE = re.compile(rf"^MSB_COLLECTION_ERROR\s+({_TEST_ID})$")

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1"""

_TEST_CMDS = r"""cd /home/opentelemetry-python
TARGETS=""
for d in @@TEST_DIRS@@; do
  if [ -d "$d" ]; then
    TARGETS="$TARGETS $d"
  fi
done
if [ -z "$TARGETS" ]; then
  echo "no test directory of this era is present" >&2
  exit 1
fi
rc=0
for d in $TARGETS; do
  s=0
  python -m pytest "$d" -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors 2>&1 | tee /tmp/msb_pytest.log || s=$?
  python - /tmp/msb_pytest.log <<'MSB_COLLECT_EOF'
import ast
import re
import sys

seen = set()
for raw in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = re.match(r"^ERROR (\S+\.py)(?: - .*)?$", raw.rstrip("\n"))
    if not m or m.group(1) in seen:
        continue
    path = m.group(1)
    seen.add(path)
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError, ValueError):
        continue
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            print("MSB_COLLECTION_ERROR %s::%s" % (path, node.name))
        elif isinstance(node, ast.ClassDef):
            bases = [getattr(b, "attr", getattr(b, "id", "")) for b in node.bases]
            if node.name.startswith("Test") or any(str(b).endswith("TestCase") for b in bases):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        print("MSB_COLLECTION_ERROR %s::%s::%s" % (path, node.name, item.name))
MSB_COLLECT_EOF
  if [ "$s" -gt 1 ]; then
    echo "pytest exited with status $s for $d" >&2
    rc="$s"
  fi
done
if [ "$rc" -gt 1 ]; then
  exit "$rc"
fi""".replace("@@TEST_DIRS@@", " ".join(_TEST_DIRS.split()))


class OpentelemetryPython2408To1863ImageBase(Image):
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
        return _PYTHON_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/80-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade {_PIP_TOOLING} && \\
    python -m pip install --no-cache-dir {_PIP_RUNTIME}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpentelemetryPython2408To1863ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return OpentelemetryPython2408To1863ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

installed=0
for d in {" ".join(_PACKAGE_DIRS.split())}; do
  if [ -f "$d/setup.py" ]; then
    python -m pip install --no-cache-dir --no-deps --no-build-isolation -e "$d"
    installed=$((installed + 1))
  fi
done
if [ "$installed" -lt 7 ]; then
  echo "prepare.sh: only $installed in-repo packages installed, expected at least 7"
  exit 1
fi

cd /tmp
python -c "import pytest, pytest_benchmark, grpc, thrift, google.protobuf; from google.rpc import error_details_pb2; from opentelemetry.sdk.trace import TracerProvider; from opentelemetry.proto.trace.v1 import trace_pb2; from opentelemetry.proto.metrics.v1 import metrics_pb2; import opentelemetry.exporter.jaeger.thrift; import opentelemetry.exporter.jaeger.proto.grpc; import opentelemetry.exporter.otlp.proto.grpc; import opentelemetry.test; print('DEPS_OK')"
cd /home/{self.pr.repo}
python -m pytest opentelemetry-sdk/tests --collect-only -q -p no:cacheprovider > /tmp/collect.log || {{ tail -30 /tmp/collect.log; echo "prepare.sh: pytest cannot collect the sdk test suite"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "OpentelemetryPython2408To1863ImageDefault dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    PYTHONUNBUFFERED=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("open-telemetry", "opentelemetry_python_2408_to_1863")
class OPENTELEMETRY_PYTHON_2408_TO_1863(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return OpentelemetryPython2408To1863ImageDefault(self.pr, self._config)

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
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            name = status = None
            m = _VERBOSE_RE.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            elif _COLLECTION_ERROR_RE.match(line):
                name, status = _COLLECTION_ERROR_RE.match(line).group(1), "ERROR"
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name or not status:
                continue
            if status == "PASSED":
                passed.add(name)
            elif status in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
