from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "95_to_89"
_PYTHON_IMAGE = "python:3.8-slim-bookworm"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_NODE_ID_RE = re.compile(r"^[^\s:]+\.py(::\S*)?$")
_VERBOSE_RE = re.compile(r"^(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_SUMMARY_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)")

_TEST_ENV = "export CI=true ICONTRACT_SLOW=true PYTHONHASHSEED=0"

_CONSTRAINTS = "\n".join(
    [
        "asttokens==2.4.1",
        "black==20.8b1",
        "certifi==2026.7.22",
        "charset-normalizer==3.5.1",
        "click==8.1.8",
        "codecov==2.1.13",
        "coverage==7.6.1",
        "exceptiongroup==1.3.1",
        "flake8==7.1.2",
        "forbiddenfruit==0.1.4",
        "icontract==2.5.0",
        "idna==3.15",
        "iniconfig==2.1.0",
        "mccabe==0.7.0",
        "mypy==1.14.1",
        "mypy_extensions==1.1.0",
        "numpy==1.24.4",
        "packaging==26.2",
        "pathspec==0.12.1",
        "pluggy==1.5.0",
        "pycodestyle==2.12.1",
        "pydocstyle==5.1.1",
        "pyflakes==3.2.0",
        "pytest==8.3.5",
        "regex==2024.11.6",
        "requests==2.32.4",
        "six==1.17.0",
        "snowballstemmer==3.1.1",
        "toml==0.10.2",
        "tomli==2.4.1",
        "typed-ast==1.5.5",
        "typing-inspect==0.9.0",
        "typing_extensions==4.13.2",
        "urllib3==2.2.3",
        "z3-solver==4.8.9.0",
    ]
)

_TEST_CMD = (
    "python -m pytest --doctest-modules -v -rA --color=no -p no:cacheprovider"
)


class CrossHairImageBase_CROSSHAIR_95_TO_89(Image):
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
        return f"base-{_TAG_SUFFIX}"

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

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99apt-retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential libtk8.6 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class CrossHairImageDefault_CROSSHAIR_95_TO_89(Image):
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
        return CrossHairImageBase_CROSSHAIR_95_TO_89(self.pr, self.config)

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

cat > /tmp/constraints.txt <<'EOF'
{_CONSTRAINTS}
EOF

python --version
export PIP_DEFAULT_TIMEOUT=120
python -m pip install --no-cache-dir -c /tmp/constraints.txt -e ".[dev]" || {{ echo "prepare.sh: pip install -e .[dev] failed at the base commit"; exit 1; }}
python -m pip freeze
test "$(python -c 'import icontract; print(icontract.__version__)')" = "2.5.0" || {{ echo "prepare.sh: icontract is not pinned to 2.5.0"; exit 1; }}
python -c "import z3; print(z3.get_version_string())" || {{ echo "prepare.sh: z3-solver does not import"; exit 1; }}
python -m pytest --version || {{ echo "prepare.sh: pytest is not installed"; exit 1; }}
python -c "import crosshair, icontract, forbiddenfruit, typing_inspect, tkinter" || {{ echo "prepare.sh: crosshair or a test dependency does not import"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{_TEST_ENV}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{_TEST_ENV}
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{_TEST_ENV}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "CrossHairImageDefault_CROSSHAIR_95_TO_89 dependency must be an Image"
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
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("pschanely", "CrossHair_95_to_89")
@Instance.register("pschanely", "CrossHair")
class CROSSHAIR_95_TO_89(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return CrossHairImageDefault_CROSSHAIR_95_TO_89(self.pr, self._config)

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
            else:
                m = _SUMMARY_RE.match(line)
                if m:
                    status, name = m.group(1), m.group(2)
            if not name or not _NODE_ID_RE.match(name):
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
