import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PYTHON_IMAGE = "python:3.8-bookworm"

_CONSTRAINTS = """\
ansible==2.10.5
ansible-base==2.10.5
Jinja2==2.11.2
MarkupSafe==1.1.1
cryptography==3.3.1
cffi==1.14.4
pycparser==2.20
six==1.15.0
PyYAML==5.3.1
pytest==6.2.1
attrs==20.3.0
iniconfig==1.1.1
packaging==20.8
pluggy==0.13.1
py==1.10.0
pyparsing==2.4.7
toml==0.10.2
setuptools_scm==5.0.1
wheel==0.36.2
"""

_TEST_CMD = (
    "rc=0\n"
    "python -m pytest tests/unit -v -p no:cacheprovider "
    "-o console_output_style=classic --continue-on-collection-errors || rc=$?\n"
    "# pytest exit 1 = some tests failed (expected in the graded stages); 2-5 mean the\n"
    "# run itself broke (interrupted, internal error, usage error, nothing collected).\n"
    'if [ "$rc" -gt 1 ]; then\n'
    '    echo "pytest exited with code $rc" >&2\n'
    '    exit "$rc"\n'
    "fi\n"
)

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"

_CHECK_GIT_CHANGES = """#!/bin/bash
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
"""


class XoperaOperaImageBase(Image):
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
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_ROOT_USER_ACTION=ignore

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class XoperaOperaImageDefault(Image):
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
        return XoperaOperaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo

        return (
            f"{_SCRIPT_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision ---\n"
            "# setup.cfg and the Pipfile pin nothing (ansible >= 2.8, pyyaml >= 3.10, pytest = *),\n"
            "# so every package this install resolves - opera's deps, ansible's deps, pytest and\n"
            "# its plugins, the build tooling - is pinned to its release as of the PR (Jan 2021).\n"
            "# Written outside the repo so the tree stays untouched by it.\n"
            "cat > /home/constraints.txt <<'__CONSTRAINTS_EOF__'\n"
            f"{_CONSTRAINTS}"
            "__CONSTRAINTS_EOF__\n"
            "\n"
            "python --version\n"
            "pip --version\n"
            "\n"
            "# setup.py derives its version with setuptools_scm (setup_requires), so the build\n"
            "# tooling is installed first and the editable install runs without build isolation\n"
            "# to use it. This writes only gitignored output (src/*.egg-info).\n"
            "installed=0\n"
            "for attempt in 1 2 3; do\n"
            "    if pip install -c /home/constraints.txt setuptools_scm wheel \\\n"
            "        && pip install -c /home/constraints.txt --no-build-isolation -e . pytest; then\n"
            "        installed=1\n"
            "        break\n"
            "    fi\n"
            '    echo "prepare: pip install attempt $attempt failed; retrying in 15s"\n'
            "    sleep 15\n"
            "done\n"
            'if [ "$installed" != 1 ]; then\n'
            '    echo "prepare: pip install failed 3 times" >&2\n'
            "    exit 1\n"
            "fi\n"
            "\n"
            "# --- 3. gate ---\n"
            "# opera must import from this checkout (not a site-packages copy), with the pinned\n"
            "# dependencies, and pytest must collect a non-empty unit suite.\n"
            'python -c "'
            "import os, opera, yaml, ansible, pytest; "
            f"p = os.path.realpath(opera.__path__[0]); print('opera ->', p); "
            f"assert p == '/home/{repo}/src/opera', p; "
            "print('PyYAML', yaml.__version__, '| pytest', pytest.__version__)"
            '"\n'
            "command -v opera\n"
            "pip check\n"
            "# Every resolved distribution must come from the constraints file (no modern stragglers).\n"
            "python - <<'__PIN_CHECK_EOF__'\n"
            "import importlib.metadata as md\n"
            "norm = lambda n: n.lower().replace('_', '-')\n"
            "pins = {norm(l.split('==')[0]) for l in open('/home/constraints.txt') if '==' in l}\n"
            "extra = sorted({norm(d.metadata['Name']) for d in md.distributions()}\n"
            "               - pins - {'pip', 'setuptools', 'opera'})\n"
            "assert not extra, 'prepare: installed packages missing from constraints: %s' % extra\n"
            "print('all installed packages pinned:', len(pins))\n"
            "__PIN_CHECK_EOF__\n"
            "collected=$(python -m pytest tests/unit --collect-only -q -p no:cacheprovider "
            "| grep -c '::')\n"
            'echo "pytest --collect-only: $collected tests"\n'
            'test "$collected" -gt 0\n'
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_TEST_CMD}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_TEST_CMD}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("xlab-si", "xopera-opera")
class XoperaOpera(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return XoperaOperaImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(
            r"^(tests/\S+\.py::.*?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$"
        )

        for line in log.splitlines():
            m = result_re.match(line.rstrip())
            if not m:
                continue
            name, status = m.group(1), m.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
