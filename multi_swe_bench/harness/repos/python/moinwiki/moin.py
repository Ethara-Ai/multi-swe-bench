import re
from typing import Optional, Union

from unidiff import PatchSet

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_DIR = "src/moin/"
_TEST_PKG = "/_tests/"

_EXCLUDED_BASENAMES = frozenset({
    "conftest.py",
    "__init__.py",
})

_PYTEST = (
    "python -m pytest --no-header -rN --tb=short -v --continue-on-collection-errors"
)

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _test_files_from_patch(patch: str) -> list[str]:
    seen: set[str] = set()
    for patched_file in PatchSet(patch):
        path = patched_file.target_file
        if path.startswith(("a/", "b/")):
            path = path[2:]
        if path == "/dev/null":
            continue
        if not (path.endswith(".py") and path.startswith(_TEST_DIR)):
            continue
        if _TEST_PKG not in path:
            continue
        basename = path.rsplit("/", 1)[-1]
        if basename not in _EXCLUDED_BASENAMES:
            seen.add(path)
    return sorted(seen)


def _test_dirs_from_patch(patch: str) -> list[str]:
    return sorted({path.rsplit("/", 1)[0] for path in _test_files_from_patch(patch)})


class moinImageBase(Image):
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
        return "python:3.12-bookworm"

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

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'

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

ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONHASHSEED=0

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends git && \\
    rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image:
        return moinImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _extra_pip_packages(self) -> list[str]:
        return []

    def _extra_import_checks(self) -> list[str]:
        return []

    def _pytest_cmd(self) -> str:
        test_dirs = " ".join(_test_dirs_from_patch(self.pr.test_patch))
        return f"{_PYTEST} {test_dirs}" if test_dirs else _PYTEST

    def _prepare_sh(self) -> str:
        extra_installs = "".join(
            f"pip install --no-cache-dir {pkg}\n"
            for pkg in self._extra_pip_packages()
        )
        import_checks = " ".join(
            f"import {mod};"
            for mod in [
                "moin",
                "pytest",
                "py",
                "lxml",
                "psutil",
                "flask",
                "whoosh",
                "markupsafe",
                *self._extra_import_checks(),
            ]
        )

        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout {self.pr.base.sha}\n"
            "bash /home/check_git_changes.sh\n"
            "\n"
            "pip install --no-cache-dir -r requirements.d/development.txt || true\n"
            "pip install --no-cache-dir pytest py lxml psutil\n"
            "pip install --no-cache-dir -e .\n"
            f"{extra_installs}"
            "\n"
            f'python -c "{import_checks}"\n'
            "python -m pytest --version\n"
            'echo "DEPS_OK"\n'
            "\n"
            f"{self._pytest_cmd()} || true\n"
        )

    def _run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            f"{self._pytest_cmd()}\n"
        )

    def _test_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{self._pytest_cmd()}\n"
        )

    def _fix_run_sh(self) -> str:
        return (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{self._pytest_cmd()}\n"
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

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register("moinwiki", "moin")
class MOIN(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        log = ansi_escape.sub("", log)

        pattern = re.compile(
            r"^(src/moin/\S+\.py::.*?)\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"(?:\s+\[\s*\d+%\])?\s*$"
        )

        for line in log.splitlines():
            m = pattern.match(line)
            if m:
                test_name = m.group(1)
                status = m.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(test_name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
