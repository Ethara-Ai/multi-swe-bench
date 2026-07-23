import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_CMD = "python -m pytest --no-header -rA --tb=no -p no:cacheprovider"


class ImageBase(Image):
    """Shared base layer: OS + Python dependencies only.

    Deliberately does NOT clone the repo, so it stays PR-independent and is
    reused by every pr-N image. It carries the BuildKit syntax directive so
    DockerfileEnhancer skips it (image.py: `if cls.SYNTAX_DIRECTIVE in raw`):
    the auto-injected hardening block begins with `git checkout --detach` and
    would fail here, since there is no working tree yet. Hardening is applied
    in ImageDefault instead, where the repo actually exists.
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
        return "python:3.7-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return (
            "# syntax=docker/dockerfile:1.6\n"
            "FROM python:3.7-bookworm\n"
            "\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "ENV LANG=C.UTF-8\n"
            "ENV TZ=UTC\n"
            "\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates curl \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            "\n"
            "RUN pip install --upgrade pip\n"
            "RUN pip install 'urllib3<2' 'pyOpenSSL<24.0.0' mock 'responses==0.18.0'\n"
            "\n"
            'CMD ["/bin/bash"]\n'
        )


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
                "prepare.sh",
                f"""ls -F
###ACTION_DELIMITER###
pip install 'urllib3<2' 'pyOpenSSL<24.0.0' mock 'responses==0.18.0'
pip install -e ".[dev]" || (pip install -e . && ([ -f requirements-dev.txt ] && pip install -r requirements-dev.txt || true))
python -m pytest --version || pip install pytest pytest-httpbin
###ACTION_DELIMITER###
{TEST_CMD}
###ACTION_DELIMITER###
echo '{TEST_CMD}' > test_commands.sh""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
{TEST_CMD}

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || {{
    echo "Warning: standard apply failed, trying with --reject" >&2
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/test.patch || true
}}
{TEST_CMD}

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{self.pr.repo}
git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch /home/fix.patch || {{
    echo "Warning: combined apply failed, trying separately" >&2
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/test.patch || git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/test.patch || true
    git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement /home/fix.patch || git -C /home/{self.pr.repo} apply --whitespace=nowarn --allow-binary-replacement --reject /home/fix.patch || true
}}
{TEST_CMD}

""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        base_image = self.dependency().image_full_name()
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
        base_commit = self.pr.base.sha

        # dependency() returns an Image, so DockerfileEnhancer returns this
        # content untouched and the harness does not supply the REPO_URL /
        # BASE_COMMIT build args (both are gated on `isinstance(dep, str)`).
        # The clone URL and commit are therefore baked in literally, and the
        # anti-reward-hacking hardening block is applied here explicitly.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", base_commit)

        return (
            f"FROM {base_image}\n"
            "\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "ENV GITHUB_ACTIONS=true\n"
            "\n"
            "WORKDIR /home/\n"
            f'RUN git clone "{repo_url}" /home/{self.pr.repo}\n'
            "\n"
            f"WORKDIR /home/{self.pr.repo}\n"
            "RUN git reset --hard\n"
            f"RUN git checkout {base_commit}\n"
            "\n"
            'RUN pip install -e ".[dev]" || (pip install -e . && ([ -f requirements-dev.txt ] && pip install -r requirements-dev.txt || true))\n'
            "RUN python -m pytest --version || pip install pytest pytest-httpbin\n"
            "\n"
            f"{hardening}\n"
            "\n"
            f"{copy_commands}"
        )


@Instance.register("httpie", "cli")
class CLI(Instance):
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
        log = re.sub(r'\x1b\[[0-9;]*m', '', log)
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        test_results = {}
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[[\d]+\])?)\s+(\S+)"
        )
        for line in log.splitlines():
            match = pattern.search(line)
            if match:
                status, test_file = match.group(1), match.group(2)
                test_file = test_file.strip()
                if "FAIL" in status or "ERROR" in status:
                    test_results[test_file] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_file) != "failed":
                        test_results[test_file] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_file) not in ["failed", "skipped"]:
                        test_results[test_file] = "passed"
        for test_file, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_file)
            elif status == "failed":
                failed_tests.add(test_file)
            elif status == "skipped":
                skipped_tests.add(test_file)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
