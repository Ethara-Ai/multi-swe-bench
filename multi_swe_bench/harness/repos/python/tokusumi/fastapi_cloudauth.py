import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "python:3.8-slim"

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
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
pytest -v -rA --disable-warnings tests/ || true
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
pytest -v -rA --disable-warnings tests/ || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
pytest -v -rA --disable-warnings tests/ || true
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {self.dependency()}
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
# pip draws its download progress bar with Unicode block characters (U+2501 ->
# E2 94 81). The harness decodes buildx output with the platform default codec,
# which on Windows is cp1252, where 0x81 is undefined - so the build dies with
# "'charmap' codec can't decode byte 0x81". Suppressing the bar keeps pip's output
# pure ASCII and the build portable.
ENV PIP_PROGRESS_BAR=off
ENV PIP_NO_COLOR=1
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset
# --hard and checkout BASE_COMMIT, then the history-scrub block - which asserts
# HEAD == BASE_COMMIT and fails the build if not. Repeating any of that here is
# dead code: a "fetch the commit if missing" fallback placed after the scrub could
# never fire, because that assertion would already have killed the build.
#
# The WORKDIR is kept deliberately. Everything below runs pip and pytest inside the
# repo, and relying on the enhancer to leave us in the right directory would be a
# silent dependency on its line ordering.
WORKDIR /home/{self.pr.repo}

# The repo ships no poetry.lock, so `poetry install` would resolve today's releases
# against Feb-2021 code (`fastapi >= 0.60.1, < 1.0` alone spans four years of
# breaking changes). Dependencies are pinned to the era of the base commit instead
# and installed with pip, so the environment is identical in all three stages.
#
# pytest-asyncio is REQUIRED here even though the base commit does not declare it:
# the fix patch adds it, and every target test is `async def` with
# @pytest.mark.asyncio. Without the plugin pytest 5.x does not run async tests, it
# SKIPS them - so the fix stage would report skips instead of passes, no test would
# transition, and the instance would score 0 for a reason unrelated to the patch.
RUN pip install --no-cache-dir -U pip setuptools wheel
RUN pip install --no-cache-dir \\
    "fastapi==0.63.0" \\
    "pydantic==1.7.3" \\
    "starlette==0.13.6" \\
    "python-jose[cryptography]==3.2.0" \\
    "requests==2.25.1" \\
    "uvicorn==0.13.4" \\
    "pytest==5.4.3" \\
    "pytest-mock==3.5.1" \\
    "pytest-asyncio==0.14.0"

# The cloud SDKs are dev-dependencies of the repo. tests/test_auth0.py,
# test_cognito.py and test_firebase.py import them at module level, and
# tests/test_cloudauth.py imports all three - so without these four of the seven
# test modules fail to collect. Their tests still error at setup_class because we
# hold no Auth0/Cognito/Firebase credentials, but that happens identically in every
# stage and therefore invents no transitions.
RUN pip install --no-cache-dir \\
    "boto3==1.17.7" \\
    "auth0-python==3.16.0" \\
    "authlib==0.15.3" \\
    "firebase-admin==4.5.2"

# Fail the build now rather than let a stage silently report zero tests: a
# half-installed environment is indistinguishable downstream from "these tests do
# not exist", which the harness scores as a valid n2p-only resolve.
RUN python -c "import fastapi, pydantic, jose, requests, pytest, pytest_asyncio, boto3, auth0, firebase_admin"
RUN pytest --collect-only -q tests/test_base.py > /dev/null

{copy_commands}"""


@Instance.register("tokusumi", "fastapi-cloudauth")
class FastapiCloudauth(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Parse only pytest's verbose form:
        #     tests/test_base.py::test_x[a b c] PASSED  [ 7%]
        #
        # Every entry is therefore a full node id (path::name), as required. Two
        # things are deliberately NOT parsed:
        #
        #   * the `-rA` short summary ("FAILED name - reason"). Splitting a name off
        #     a reason is ambiguous when a parametrize id itself contains " - ", and a
        #     truncated name would land in the set alongside the correct one from the
        #     verbose line, double-counting the same test.
        #   * collection errors ("ERROR tests/test_base.py"). Those name a FILE that
        #     failed to import, not a test, so recording them as tests would put a
        #     non-node-id into the report. The tests inside were never discovered, so
        #     their absence is the correct signal.
        #
        # The name is captured non-greedily up to the status word, so parametrize ids
        # containing spaces survive intact.
        verbose = re.compile(
            r"^(\S+\.py::.+?)\s+"
            r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\[[^\]]*\])?\s*$"
        )

        for raw in log.split("\n"):
            m = verbose.match(raw.rstrip())
            if not m:
                continue
            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR", "XPASS"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # A test can appear twice (verbose line plus -rA summary, or a rerun).
        # Fix the precedence so it lands in exactly one bucket, otherwise the
        # stage comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
