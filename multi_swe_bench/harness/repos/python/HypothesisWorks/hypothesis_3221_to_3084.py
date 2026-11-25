from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "hypothesis_3221_to_3084"
_BASE_IMAGE = "python:3.8.20-bookworm"
_BASE_TAG = "base-3221_to_3084"
_PYTHON_VERSION = "3.8"

_ENV_SH = """export CI=true
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PIP_ROOT_USER_ACTION=ignore
export PATH="/opt/venv/bin:$PATH"
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

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__BASE_SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__BASE_SHA__"
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__ENV__
python -m venv /opt/venv
python -m pip install --no-cache-dir --upgrade "pip<25" wheel
__DEPS__
pip install --no-cache-dir -r requirements/test.txt || true
pip install --no-cache-dir -r requirements/coverage.txt || true
sed -E 's/^([A-Za-z0-9_.-]+)\\[[^]]*\\]/\\1/' requirements/coverage.txt > /tmp/mswe-constraints.txt
pip install --no-cache-dir -c /tmp/mswe-constraints.txt -e hypothesis-python/ || true
__EXTRA_DEPS__git checkout -- .
git clean -fd -e '*.egg-info'

python -c "import sys, hypothesis, pytest, xdist, numpy; assert sys.version.startswith('__PYTHON_VERSION__.'), sys.version; assert '/home/__REPO__/hypothesis-python/src' in hypothesis.__file__, hypothesis.__file__; print('DEPS_OK', hypothesis.__version__, pytest.__version__)"
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
"""

_BODY_SH = """TARGETS=(__TARGETS__)
RUN_TARGETS=()
for target in "${TARGETS[@]}"; do
    if [ -f "hypothesis-python/$target" ]; then
        RUN_TARGETS+=("$target")
    fi
done
if [ ${#RUN_TARGETS[@]} -eq 0 ]; then
    echo "MSWE: no target test files present"
    exit 0
fi

cd hypothesis-python
timeout --kill-after=60 5400 python -bb -X dev -m pytest -p no:cacheprovider -n 4 -v -rA "${RUN_TARGETS[@]}"
"""

_BINARY_NO_DATA_RE = re.compile(r"^Binary files (?:a/\S+|/dev/null) and b/(\S+) differ$", re.M)
_DIFF_FILE_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_TEST_ROOT = "hypothesis-python/"
_REQUIREMENT_FILES = ("requirements/test.txt", "requirements/coverage.txt")
_RESULT_RE = re.compile(
    r"^(?:\[gw\d+\] \[\s*\d+%\] )?(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+?\.py::.+?)(?: - .*)?\s*$"
)
_XDIST_RE = re.compile(r"^\[gw\d+\] \[\s*\d+%\] (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+?\.py::.+?)\s*$")
_COLLECT_ERROR_RE = re.compile(r"^ERROR (\S+?\.py)(?: - .*)?\s*$")
_RESULT_SUFFIX_RE = re.compile(r"^(\S+?\.py::.+?) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STATUS = {
    "PASSED": "pass",
    "XPASS": "pass",
    "FAILED": "fail",
    "ERROR": "fail",
    "SKIPPED": "skip",
    "XFAIL": "skip",
}


def _apply(pr: PullRequest) -> str:
    paths = sorted(
        set(_BINARY_NO_DATA_RE.findall(pr.test_patch or ""))
        | set(_BINARY_NO_DATA_RE.findall(pr.fix_patch or ""))
    )
    excludes = "".join(f" --exclude={shlex.quote(path)}" for path in paths)
    return f"git apply --whitespace=nowarn{excludes}"


def _apply_with_fallback(pr: PullRequest, patches: str) -> str:
    apply = _apply(pr)
    steps = "\n".join(
        f"    {apply} {p} || {{ git add -A && {apply} --3way {p}; }} || {{ echo \"MSWE: cannot apply {p}\" >&2; exit 2; }}"
        for p in patches.split()
    )
    return f"if ! {apply} {patches}; then\n    git checkout -- .\n    git clean -fdq\n{steps}\nfi"


_RELATED_TESTS: dict[int, list[str]] = {}


def _targets(pr: PullRequest) -> str:
    targets: list[str] = []
    for path in _DIFF_FILE_RE.findall(pr.test_patch or ""):
        if not path.startswith(_TEST_ROOT + "tests/"):
            continue
        rel = path[len(_TEST_ROOT):]
        if rel.startswith("tests/django/"):
            continue
        if rel.startswith("tests/ghostwriter/recorded/"):
            rel = "tests/ghostwriter/test_expected_output.py"
        name = rel.rsplit("/", 1)[-1]
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        if rel not in targets:
            targets.append(rel)
    for rel in _RELATED_TESTS.get(pr.number, []):
        if rel not in targets:
            targets.append(rel)
    return " ".join(shlex.quote(t) for t in targets)


def _extra_deps(pr: PullRequest) -> str:
    if "tests/ghostwriter/" not in _targets(pr):
        return ""
    return (
        "if ! python -c 'import black' 2> /dev/null; then\n"
        "    sed -E 's/^([A-Za-z0-9_.-]+)\\[[^]]*\\]/\\1/' requirements/tools.txt > /tmp/mswe-tools-constraints.txt\n"
        "    pip install --no-cache-dir -c /tmp/mswe-tools-constraints.txt black\n"
        "fi\n"
        "python -c 'import black'\n"
    )


def _deps(pr: PullRequest) -> str:
    lines = []
    for patch_path, patch in (("/home/test.patch", pr.test_patch), ("/home/fix.patch", pr.fix_patch)):
        files = [f for f in _REQUIREMENT_FILES if f in _DIFF_FILE_RE.findall(patch or "")]
        if files:
            includes = " ".join(f"--include={shlex.quote(f)}" for f in files)
            lines.append(f"git apply --whitespace=nowarn {includes} {patch_path}")
    return "\n".join(lines)


class Hypothesis3221To3084ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo} multi-swe-bench base"
LABEL org.opencontainers.image.description="Shared base image for {org}/{repo} {_INTERVAL} instances"
LABEL org.opencontainers.image.source="https://github.com/{org}/{repo}"
LABEL org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{DockerfileEnhancer._MITM_MOUNT}

{self.global_env}

RUN if ! command -v git > /dev/null || ! command -v make > /dev/null || ! command -v gcc > /dev/null; then \\
        apt-get update && apt-get install -y --no-install-recommends ca-certificates git make build-essential \\
        && rm -rf /var/lib/apt/lists/*; \\
    fi

RUN python -c "import sys; assert sys.version.startswith('{_PYTHON_VERSION}.'), sys.version"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Hypothesis3221To3084ImageDefault(Image):
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
        return Hypothesis3221To3084ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _script(self, template: str) -> str:
        return (
            template.replace("__ENV__", _ENV_SH)
            .replace("__DEPS__", _deps(self.pr))
            .replace("__EXTRA_DEPS__", _extra_deps(self.pr))
            .replace("__TARGETS__", _targets(self.pr))
            .replace("__PYTHON_VERSION__", _PYTHON_VERSION)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        header = self._script(_HEADER_SH)
        body = self._script(_BODY_SH)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._script(_PREPARE_SH)),
            File(".", "run.sh", f"{header}\n{body}"),
            File(".", "test-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch')}\n\n{body}"),
            File(".", "fix-run.sh", f"{header}\n{_apply_with_fallback(self.pr, '/home/test.patch /home/fix.patch')}\n\n{body}"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        return f"""FROM {image.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


def _parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}

    lines = [line.strip() for line in _ANSI_RE.sub("", test_log).replace("\r", "\n").split("\n")]
    use_xdist = any(_XDIST_RE.match(line) for line in lines)
    for line in lines:
        match = _XDIST_RE.match(line)
        if match:
            status, name = match.group(1), match.group(2)
        else:
            match = _COLLECT_ERROR_RE.match(line)
            if match:
                status, name = "ERROR", match.group(1)
            elif use_xdist:
                continue
            else:
                match = _RESULT_RE.match(line)
                if match:
                    status, name = match.group(1), match.group(2)
                else:
                    match = _RESULT_SUFFIX_RE.match(line)
                    if not match:
                        continue
                    name, status = match.group(1), match.group(2)
        if name.startswith("tests/"):
            name = _TEST_ROOT + name
        if not name.startswith(_TEST_ROOT + "tests/"):
            continue
        buckets[_STATUS[status]].add(name)

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


@Instance.register("HypothesisWorks", _INTERVAL)
class HYPOTHESIS_3221_TO_3084(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Hypothesis3221To3084ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_log(test_log)
