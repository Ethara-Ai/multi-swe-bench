import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Repo-level base image: OS deps + cloned/checked-out source + baked env.
    Built once as `<repo>:base`; PR images layer only patches + scripts on top."""

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
        # renku-python at this (2020-era) commit targets Python 3.7/3.8; 3.10 breaks
        # old ruamel.yaml's C extension (removed CPython APIs). Use 3.8.
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "envagent"

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

        # Clone via "${{REPO_URL}}" (the pipeline enhancer leaves this form untouched and
        # injects its git-hardening block just before our trailing CMD); env installs sit
        # after checkout so `pip install -e .[tests,docs]` sees the checked-out source.
        return f"""FROM {image_name}
ENV DEBIAN_FRONTEND=noninteractive
# renku's `show inputs/outputs` tests assert on click CliRunner output, which mixes stderr
# into stdout. PyYAML 5.x emits a YAMLLoadWarning for renku's loaderless `yaml.load()` calls,
# and that warning line pollutes the captured output -> the target f2p test (test_show_inputs)
# fails even in the fix stage. Suppressing warnings lets the PR's real behavior change surface:
# test_show_inputs FAILS at test stage, PASSES at fix stage (clean fail->pass).
ENV PYTHONWARNINGS=ignore
# lxml (pulled by renku's [tests,docs] extras) needs libxml2/libxslt dev headers;
# libffi/libssl/zlib cover cryptography/pillow-style native builds in the same tree.
RUN apt-get update && apt-get install -y git build-essential curl \\
    libxml2-dev libxslt1-dev libffi-dev libssl-dev zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
# Base image `git clone` can land an incomplete packfile; fetch the base commit by URL if missing.
RUN git cat-file -e ${{BASE_COMMIT}} 2>/dev/null || git fetch --no-tags "${{REPO_URL}}" ${{BASE_COMMIT}}
RUN git checkout ${{BASE_COMMIT}}

# --- Environment baked in so human_mode=True works (recipe from prepare.sh notes).
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir 'setuptools<58.0.0' wheel
# renku's install_requires pins cwltool==1.0.20180820141117 which forces ruamel.yaml<=0.15.51.
# That ruamel's setup.py does `from _ast import *`, but CPython 3.8 dropped Str/Num/Bytes from the
# _ast C module (moved to ast.py as deprecated Constant aliases) -> `NameError: name 'Str'`.
# Repoint the import to `ast` and pre-build/install it so the later editable install is satisfied.
RUN cd /tmp \\
    && curl -sL https://files.pythonhosted.org/packages/77/19/c225d7dd6b3678e5f8b76b8101dc903a0f1799b7182eeab4d20b07a32878/ruamel.yaml-0.15.51.tar.gz -o ruamel.tgz \\
    && tar xzf ruamel.tgz && cd ruamel.yaml-0.15.51 \\
    && sed -i 's/^from _ast import \\*/from ast import */' setup.py \\
    && pip install --no-cache-dir --no-build-isolation . \\
    && cd / && rm -rf /tmp/ruamel.yaml-0.15.51 /tmp/ruamel.tgz
RUN pip install --no-cache-dir rdflib-jsonld==0.4.0
# renku declares `pyld>=1.0.3` (unbounded) so pip grabs pyld 3.x, whose jsonld.py has a
# module-level `Callable[[str | None], Any]` (PEP 604) that dies on Python 3.8. Pin to the
# renku-era pyld; pre-installing it satisfies >=1.0.3 so the editable install won't upgrade.
RUN pip install --no-cache-dir pyld==1.0.5
# renku (and its tests) call `yaml.load(x)` without a Loader; PyYAML 6.x makes Loader required
# -> `TypeError: load() missing 1 required positional argument`. renku pins only PyYAML>=3.12
# (unbounded), so pip grabs 6.x. Pin the last 5.x where load-without-Loader still works.
RUN pip install --no-cache-dir "PyYAML==5.4.1"
RUN pip install --no-cache-dir -e .[tests,docs]
RUN pip install --no-cache-dir attrs==19.3.0
# renku's pytest.ini enables flake8/pep8/yapf plugins that break collection; strip them.
RUN sed -i 's/--yapf//; s/--flake8 --pep8 //; s/--flake8//; s/--pep8//' pytest.ini || true
RUN pip uninstall -y pytest-pep8 pytest-flake8 pytest-yapf || true
# renku's test fixtures run `renku init` (a git commit) which aborts without an identity,
# surfacing as `assert 1 == 0` (exit_code != 0) errors in fixture setup for ~half the suite.
RUN git config --global user.email "renku@example.com" \\
    && git config --global user.name "Renku Test" \\
    && git config --global init.defaultBranch master

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                "check_git_changes.sh",
                """#!/bin/bash
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
                """#!/bin/bash
set -e
# Re-assert the base commit at PR-build time. Non-destructive on purpose: no `git reset`
# (this base carries an intentional working-tree edit — pytest.ini stripped of flake8/pep8/yapf)
# and no test run (tests execute at instance time via run/test/fix-run.sh).
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v -rA --no-cov

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA --no-cov

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn  /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA --no-cov

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("SwissDataScienceCenter", "renku_python_369_to_307")
class RENKU_PYTHON_369_TO_307(Instance):
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
        # Parse the log content and extract test execution results.
        passed_tests = set()  # Tests that passed successfully
        failed_tests = set()  # Tests that failed
        skipped_tests = set()  # Tests that were skipped
        import re
        import json

        # Extract test cases using regex
        # Extract test cases where test name comes first
        pattern1 = re.compile(
            r"(tests/[^:]+::[^ ]+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAILED)"
        )
        matches1 = pattern1.findall(log)
        # Extract test cases where status comes first
        pattern2 = re.compile(
            r"(PASSED|FAILED|ERROR|SKIPPED|XFAILED)\s+(tests/[^:]+::[^ ]+)"
        )
        matches2 = pattern2.findall(log)
        for test_name, status in matches1:
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ["FAILED", "ERROR", "XFAILED"]:
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
        for status, test_name in matches2:
            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ["FAILED", "ERROR", "XFAILED"]:
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)
        parsed_results = {
            "passed_tests": passed_tests,
            "failed_tests": failed_tests,
            "skipped_tests": skipped_tests,
        }

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
