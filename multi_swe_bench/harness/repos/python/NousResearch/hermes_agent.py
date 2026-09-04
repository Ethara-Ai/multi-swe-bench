import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "python:3.11"

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

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
        return ImageBase(self.pr, self._config)

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

""".format(),
            ),
            File(
                ".",
                "wire_test_worktree.py",
                '''import sys

path = sys.argv[1]
with open(path) as f:
    content = f.read()

start_marker = (
    "# ---------------------------------------------------------------------------\\n"
    "# Lightweight reimplementations for testing (avoid importing cli.py)\\n"
    "# ---------------------------------------------------------------------------\\n"
)
end_marker = (
    "# ---------------------------------------------------------------------------\\n"
    "# Tests\\n"
    "# ---------------------------------------------------------------------------\\n"
)

if start_marker not in content or end_marker not in content:
    sys.exit(0)

start_idx = content.index(start_marker)
end_idx = content.index(end_marker)

replacement = \'\'\'# ---------------------------------------------------------------------------
# Adapters wired to the real implementation in cli.py (added by fix.patch)
# ---------------------------------------------------------------------------

import subprocess as _subprocess

try:
    import cli as _cli
except Exception:
    _cli = None


def _git_repo_root(cwd=None):
    if cwd is None:
        if _cli is None:
            raise ImportError("cli module not importable")
        return _cli._git_repo_root()
    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
            cwd=cwd,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _setup_worktree(repo_root):
    if _cli is None:
        raise ImportError("cli module not importable")
    return _cli._setup_worktree(repo_root)


def _cleanup_worktree(info):
    if _cli is None:
        raise ImportError("cli module not importable")
    from pathlib import Path
    existed_before = Path(info["path"]).exists()
    _cli._cleanup_worktree(info)
    still_exists = Path(info["path"]).exists()
    if not existed_before:
        return None
    return not still_exists


\'\'\'

new_content = content[:start_idx] + replacement + content[end_idx:]
with open(path, "w") as f:
    f.write(new_content)
''',
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1
python -V

python -m pip install --no-cache-dir --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -e ".[dev]"
python -m pip install --no-cache-dir pytest-xdist
python -m pytest tests --collect-only -q -p no:cacheprovider -n 0

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
python -m pytest tests \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f tests/test_worktree.py ]; then
    python3 /home/wire_test_worktree.py tests/test_worktree.py
fi
python -m pytest tests \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f tests/test_worktree.py ]; then
    python3 /home/wire_test_worktree.py tests/test_worktree.py
fi
python -m pytest tests \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ENV BASE_COMMIT={self.pr.base.sha}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("NousResearch", "hermes-agent")
class NousResearchHermesAgent(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        re_standard = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"(?:\s+\[.*\])?\s*$"
        )

        re_xdist = re.compile(
            r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+(\S+::\S+)"
        )

        re_summary = re.compile(
            r"^(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+"
            r"(?:\[\d+\]\s+)?(\S+::\S+)"
        )

        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_xdist.match(line) or re_summary.match(line)
            if m:
                status = m.group(1)
                test_name = m.group(2)
            else:
                m = re_standard.match(line)
                if m:
                    test_name = m.group(1)
                    status = m.group(2)
                else:
                    continue

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

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
