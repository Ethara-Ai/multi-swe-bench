import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_CMD = "python -m pytest -rA --tb=no -p no:cacheprovider opsdroid tests --timeout=30 --continue-on-collection-errors"


class ImageBase(Image):
    """Heavy, self-contained environment image (``base-pr-<N>``).

    Owns the runtime, the apt toolchain, the clone, the ``BASE_COMMIT`` pin and
    the git-history scrub. Inherits ``Image.dockerfile()`` so the canonical
    section order is produced by the harness itself -- ``DockerfileEnhancer``
    then injects the syntax directive, build/proxy ARGs, ENV block, OCI labels
    and the CA-cert symlink farm directly after ``FROM``, ahead of every
    network ``RUN``.
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
        return "python:3.8-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        # The inherited default set (ca-certificates, curl, build-essential,
        # git, gnupg, make, python3, sudo, wget) already covers this stack:
        # git for the clone, ca-certificates for TLS through the proxy, and
        # build-essential for the C extensions in the 2020-era dependency set.
        return []


class ImageDefault(Image):
    """Thin PR layer (``pr-<N>``) built on top of :class:`ImageBase`.

    Stages the two patches, the integrity guard, ``prepare.sh`` and the three
    graded run-scripts, then runs ``prepare.sh`` exactly once. It deliberately
    does not clone, apt-install or scrub -- those guarantees are inherited.
    ``DockerfileEnhancer.enhance()`` returns this Dockerfile untouched because
    ``dependency()`` yields an ``Image`` rather than a string.
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is not clean" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: working tree is clean"

""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}

git reset --hard
bash /home/check_git_changes.sh

git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

# Warm the environment. Every install is `|| true`: a native wheel that fails
# to build on one architecture must not abort the image build.
pip install --upgrade pip || true
pip install "jinja2<3.1" || true
pip install -r requirements.txt || true
grep -v deadlinks requirements_test.txt > /tmp/test_reqs.txt && pip install -r /tmp/test_reqs.txt || true
pip install pytest pytest-timeout pytest-asyncio asynctest || true
pip install -e . || true

""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
{TEST_CMD}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply failed for test.patch" >&2
        exit 1
    fi
fi
{TEST_CMD}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply failed for test.patch" >&2
        exit 1
    fi
    if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn --3way /home/fix.patch; then
        echo "Error: git apply failed for fix.patch" >&2
        exit 1
    fi
fi
{TEST_CMD}

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("opsdroid", "opsdroid_1608_to_1348")
class OPSDROID_1608_TO_1348(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?|XFAIL|XPASS)\s+(.+?)(?:\s+-\s+.*)?$"
        )
        for line in log.splitlines():
            line = line.strip()
            match = pattern.match(line)
            if match:
                status = match.group(1)
                test_name = match.group(2).strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"
        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
