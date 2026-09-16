import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_PYTEST_CMD = (
    'python -m pytest --no-header -rA --tb=no -p no:cacheprovider -o addopts=""'
)


_PIP_INSTALL = (
    'pip install --no-cache-dir '
    '--uploaded-prior-to "$(git show -s --format=%cI HEAD)" -e \'.[test]\''
)


_RELEASE_SCRIPT_SIGPIPE_FIX = r'''rel_script=.github/workflows/scripts/create-release-packages.sh
if [ -f "$rel_script" ]; then
  sed -i "s
  if grep -qF "printf '%s\n' \"\$file_content\" | awk" "$rel_script"; then
    echo "create-release-packages.sh: SIGPIPE workaround not applied" >&2
    exit 1
  fi
  bash -n "$rel_script"
fi
'''


class SpecKit2259To1669ImageBase(Image):
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
        return "python:3.11-slim-bookworm"

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

        infra = DockerfileEnhancer._infrastructure_block(self, image_name).rstrip("\n")

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infra}

{self.global_env}


WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates zip unzip && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
{self.clear_env}

CMD ["/bin/bash"]
"""


class SpecKit2259To1669ImageDefault(Image):
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
        return SpecKit2259To1669ImageBase(self.pr, self.config)

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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir pip==26.2.1
{pip_install}
{release_script_fix}python -c "import specify_cli"
python -m pytest --version
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    pip_install=_PIP_INSTALL,
                    release_script_fix=_RELEASE_SCRIPT_SIGPIPE_FIX,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
{pytest_cmd}
""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
{pytest_cmd}
""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{pytest_cmd}
""".format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        fetch_base = (
            'RUN git fetch --no-tags origin "${BASE_COMMIT}"\n'
            if self.pr.base.ref != "main"
            else ""
        )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
{fetch_base}RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}

"""


_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PASSED = re.compile(r"^PASSED (.+?)\s*$", re.MULTILINE)
_FAILED = re.compile(r"^(?:FAILED|ERROR|XPASS) (.+?)(?: - .*)?\s*$", re.MULTILINE)
_SKIPPED = re.compile(r"^SKIPPED\s+\[\d+\]\s+(\S+?):\s", re.MULTILINE)


def parse_spec_kit_pytest_log(log: str) -> TestResult:
    log = _ANSI.sub("", log)

    passed_tests = set(_PASSED.findall(log))
    failed_tests = set(_FAILED.findall(log))
    skipped_tests = set(_SKIPPED.findall(log))

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


@Instance.register("github", "spec-kit_2259_to_1669")
class SPEC_KIT_2259_TO_1669(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:  # type: ignore
        return SpecKit2259To1669ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:  # type: ignore
        return parse_spec_kit_pytest_log(log)
