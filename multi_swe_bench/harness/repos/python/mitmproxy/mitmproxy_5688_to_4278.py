from __future__ import annotations

import re
import shlex

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_STATUSES = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_TRAILING_RE = re.compile(
    r"^(?P<name>test/\S+?)\s+(?P<status>" + "|".join(_STATUSES) + r")\b"
)
_LEADING_RE = re.compile(
    r"^(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?(?P<status>"
    + "|".join(_STATUSES)
    + r")\s+(?P<name>test/\S+)"
)
_SKIP_SUMMARY_RE = re.compile(r"^SKIPPED \[\d+\] (?P<name>test/[^:\s]+:\d+)")


def _test_files(test_patch: str) -> list[str]:
    files = []
    for path in _DIFF_HEADER_RE.findall(test_patch):
        name = path.rsplit("/", 1)[-1]
        if path.startswith("test/") and name.startswith("test_") and name.endswith(".py"):
            if path not in files:
                files.append(path)
    return files


def _test_command(test_patch: str) -> str:
    modules = " ".join(shlex.quote(path) for path in _test_files(test_patch))
    return f"""files=()
for module in {modules}; do
    if [ -f "$module" ]; then
        files+=("$module")
    fi
done
if [ ${{#files[@]}} -eq 0 ]; then
    echo "no test modules to run"
    exit 0
fi
python -m pytest -p no:cacheprovider -p no:cov --timeout 600 -rA -v "${{files[@]}}"
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes.sh: not inside a git work tree" >&2
    exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=dirty) ]]; then
    echo "check_git_changes.sh: unexpected changes in the work tree" >&2
    git status --porcelain | head -20
    exit 1
fi

echo "check_git_changes.sh: no changes in the work tree"
exit 0
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

BASE_DATE="$(TZ=UTC git log -1 --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD)"
echo "resolving dependencies released before ${BASE_DATE}"

python -m venv /home/tools
/home/tools/bin/pip install --no-cache-dir "uv==0.12.6"
python -m venv /home/venv

cat > /home/era-requirements.in <<EOF
__REPO__[dev] @ file:///home/__REPO__
setuptools
wheel
EOF

/home/tools/bin/uv pip compile --no-cache --python /home/venv/bin/python \\
    --exclude-newer "${BASE_DATE}" \\
    --no-emit-package __REPO__ --no-emit-package pyinstaller \\
    -o /home/era-requirements.txt /home/era-requirements.in
cat /home/era-requirements.txt
/home/tools/bin/uv pip install --no-cache --python /home/venv/bin/python \\
    --exclude-newer "${BASE_DATE}" -r /home/era-requirements.txt
/home/venv/bin/pip install --no-cache-dir --no-deps --no-build-isolation -e .

/home/venv/bin/python -c "import mitmproxy, mitmproxy.tools.main, mitmproxy.addons, pytest, pytest_asyncio, pytest_timeout, hypothesis"
/home/venv/bin/mitmdump --version
/home/venv/bin/python -m pytest --version
"""

_HEADER_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONDONTWRITEBYTECODE=1
export PATH="/home/venv/bin:$PATH"

cd /home/__REPO__
"""


class Mitmproxy5688To4278ImageBase(Image):
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
        return "python:3.9-slim-bookworm"

    def image_tag(self) -> str:
        interval = self.pr.number_interval.removeprefix(f"{self.pr.repo}_")
        return f"base-{interval}"

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
        git build-essential libffi-dev libssl-dev zlib1g-dev pkg-config \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Mitmproxy5688To4278ImageDefault(Image):
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
        return Mitmproxy5688To4278ImageBase(self.pr, self._config)

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
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
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

    test_log = _ANSI_RE.sub("", test_log)
    for raw in test_log.splitlines():
        line = raw.strip()
        status = None
        name = None
        match = _TRAILING_RE.match(line) or _LEADING_RE.match(line)
        if match:
            status = match.group("status")
            name = match.group("name")
        else:
            skip = _SKIP_SUMMARY_RE.match(line)
            if skip:
                status = "SKIPPED"
                name = skip.group("name")
        if not name or ("::" not in name and status != "SKIPPED"):
            continue
        if status in ("PASSED", "XFAIL", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)

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


@Instance.register("mitmproxy", "mitmproxy_5688_to_4278")
class MITMPROXY_5688_TO_4278(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Mitmproxy5688To4278ImageDefault(self.pr, self._config)

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
