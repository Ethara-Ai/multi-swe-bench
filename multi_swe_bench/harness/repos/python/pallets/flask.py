import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import TestStatus, mapping_to_testresult


class FlaskImageBase(Image):
    """Shared full-history base for pallets/flask 3.x (python:3.12-slim).

    flask 3.x is pyproject-only and requires-python >=3.10, so a modern pip is
    needed. Full history is kept (no BASE_COMMIT checkout / prune here) so every
    PR's base.sha stays reachable; per-PR checkout + hardening run below.
    """

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
        return "python:3.12-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        # The `# syntax` line opts this shared base out of the DockerfileEnhancer,
        # which would otherwise inject `git checkout ${BASE_COMMIT}` + ref-strip +
        # `git gc --prune` here, pruning the shared base down to one PR's base.sha
        # and breaking every other PR in the era with "reference is not a tree".
        return f"""# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class FlaskImageDefault(Image):
    """Per-PR image: check out this PR's base.sha and install flask editable."""

    # Required invariants for dataset integrity: identical across run/test/fix,
    # never a git commit (hardening asserts HEAD == base.sha), and a no-op unless
    # a class-scope `ParamType[... ssl.SSLContext]` lacks a module-scope import.
    _SSL_IMPORT_SHIM = (
        'if [ -f src/flask/cli.py ] '
        '&& grep -qE "^[[:space:]]*class .*ParamType\\[.*ssl\\.SSLContext" src/flask/cli.py '
        '&& ! grep -qE "^import ssl$" src/flask/cli.py; then '
        'if grep -qE "^from __future__ import annotations$" src/flask/cli.py; then '
        'sed -i "/^from __future__ import annotations$/a import ssl" src/flask/cli.py; '
        'else sed -i "1i import ssl" src/flask/cli.py; fi; fi'
    )

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
        return FlaskImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            # Harness replays images/pr-N/prepare.sh before every phase, falling
            # back to stale on-disk files if none is generated; emit a clean one.
            # Only the ssl shim (uncommitted, identical across phases) - no git
            # checkout (hardening stripped the history) so no spurious fail-to-pass.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{ssl_shim}
""".format(pr=self.pr, ssl_shim=self._SSL_IMPORT_SHIM),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{ssl_shim}
pytest -v -rA --tb=short -p no:cacheprovider 2>&1 || true
""".format(pr=self.pr, ssl_shim=self._SSL_IMPORT_SHIM),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{ssl_shim}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
pytest -v -rA --tb=short -p no:cacheprovider 2>&1 || true
""".format(pr=self.pr, ssl_shim=self._SSL_IMPORT_SHIM),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{ssl_shim}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch || true
pytest -v -rA --tb=short -p no:cacheprovider 2>&1 || true
""".format(pr=self.pr, ssl_shim=self._SSL_IMPORT_SHIM),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        dep_ref = f"{dep.image_name()}:{dep.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Anti-cheat hardening runs in the PR layer (the shared base keeps full
        # history so every PR's base.sha is reachable). This layer checks out this
        # PR's base.sha and installs flask at it; the canonical hardening block
        # then detaches at that literal sha and strips every other ref/reflog so
        # later commits (the fix) are unreachable. It touches only git state, so
        # the installed package is unaffected.
        hardening = Image._HARDENING_BLOCK.replace(
            "${BASE_COMMIT}", self.pr.base.sha
        ).rstrip("\n")

        return """# syntax=docker/dockerfile:1.6
FROM {dep_ref}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install -e .
RUN pip install "pytest==7.4.4" "asgiref==3.7.2" "python-dotenv==0.21.1"

{copy_commands}

{hardening}

CMD ["/bin/bash"]
""".format(pr=self.pr, copy_commands=copy_commands, dep_ref=dep_ref, hardening=hardening)


@Instance.register("pallets", "flask")
class Flask(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlaskImageDefault(self.pr, self._config)

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
        test_status_map = {}
        for line in log.split("\n"):
            if any([line.startswith(x.value) for x in TestStatus]):
                if line.startswith(TestStatus.FAILED.value):
                    line = line.replace(" - ", " ")
                test_case = line.split()
                if len(test_case) <= 1:
                    continue
                test_status_map[test_case[1]] = test_case[0]

        return mapping_to_testresult(test_status_map)
