from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PYTHON_IMAGE = "python:3.9-bookworm"
_BASE_TAG = "base-8452_to_4938"
_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

_PIP_TOOLING = '"pip==23.3.2" "wheel==0.43.0"'

_ERA_PINS = """0.13.0a0 torch==1.12.0 numpy==1.22.4 scipy==1.8.1 pillow==9.1.1 setuptools==59.6.0
0.14.0a0 torch==1.13.0 numpy==1.22.4 scipy==1.8.1 pillow==9.1.1 setuptools==59.6.0
0.19.0a0 torch==2.4.0 numpy==1.26.4 scipy==1.13.1 pillow==10.3.0 setuptools==69.5.1"""

_PIP_COMMON = (
    '"typing-extensions==4.12.2" "filelock==3.15.4" "fsspec==2024.6.1" '
    '"jinja2==3.1.4" "MarkupSafe==2.1.5" "networkx==3.2.1" "sympy==1.13.1" '
    '"mpmath==1.3.0" "requests==2.32.3" "charset-normalizer==3.3.2" "idna==3.7" '
    '"urllib3==2.2.2" "certifi==2024.7.4" "pytest==7.4.4" "pytest-mock==3.12.0" '
    '"expecttest==0.2.1" "iniconfig==2.0.0" "packaging==24.1" "pluggy==1.5.0" '
    '"exceptiongroup==1.2.2" "tomli==2.0.1" "ninja==1.11.1.1"'
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TEST_ID = r"[^\s:]+\.py::.+?"
_STATUS = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
_VERBOSE_RE = re.compile(
    rf"^[ \t]*({_TEST_ID})(?:[ \t]+<-[ \t]+\S+)?[ \t]+({_STATUS})(?:[ \t]+\(.*\))?(?:[ \t]+\[[ \t]*\d+%\])?[ \t]*$",
    re.M,
)
_SUMMARY_RE = re.compile(
    rf"^[ \t]*({_STATUS})[ \t]+({_TEST_ID})(?:[ \t]+-[ \t].*)?[ \t]*$",
    re.M,
)

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1"""

_TEST_CMD = """status=0
python -m pytest test -v -rA --tb=no --color=no -p no:cacheprovider --continue-on-collection-errors -k "not vit_h_14 and not regnet_y_128gf" --deselect test/test_backbone_utils.py::TestFxFeatureExtraction::test_forward_backward --deselect test/test_backbone_utils.py::TestFxFeatureExtraction::test_jit_forward_backward 2>&1 || status=$?
test "$status" -le 1 || { echo "pytest exited with status $status" >&2; exit "$status"; }"""


class Vision8452To4938ImageBase(Image):
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
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

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
    libpng-dev libjpeg62-turbo-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade {_PIP_TOOLING}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class Vision8452To4938ImageDefault(Image):
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
        return Vision8452To4938ImageBase(self.pr, self.config)

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

tv_version="$(tr -d '[:space:]' < version.txt)"
row="$(awk -v v="$tv_version" '$1 == v' <<'MSB_ERA_PINS'
{_ERA_PINS}
MSB_ERA_PINS
)"
test -n "$row" || {{ echo "prepare.sh: no dependency pins for torchvision $tv_version" >&2; exit 1; }}
read -r _ torch_pin era_pins <<< "$row"

python -m pip install --no-cache-dir --no-deps --index-url {_TORCH_INDEX} "$torch_pin"
python -m pip install --no-cache-dir $era_pins {_PIP_COMMON}

python setup.py develop --no-deps

cd /tmp
python -c "import torch, torchvision, numpy, scipy, PIL, pytest, pytest_mock, expecttest; torch.ops.torchvision.nms; torch.ops.image.decode_png; print('DEPS_OK', torch.__version__, torchvision.__version__)"
cd /home/{self.pr.repo}
python -m pytest test --collect-only -q -p no:cacheprovider > /tmp/collect.log 2>&1 || {{ tail -30 /tmp/collect.log; echo "prepare.sh: pytest cannot collect the test suite" >&2; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "test-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
{_RUN_HEADER}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "fix-run.sh: git apply failed" >&2; exit 1; }}
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {image.image_name()}:{image.image_tag()}

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


@Instance.register("pytorch", "vision_8452_to_4938")
class VISION_8452_TO_4938(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Vision8452To4938ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {
            "PASSED": passed,
            "FAILED": failed,
            "ERROR": failed,
            "SKIPPED": skipped,
            "XFAIL": skipped,
            "XPASS": skipped,
        }

        text = _ANSI_RE.sub("", test_log)
        pairs = list(_VERBOSE_RE.findall(text))
        pairs += [(name, status) for status, name in _SUMMARY_RE.findall(text)]
        for name, status in pairs:
            buckets[status].add(name.strip())

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
