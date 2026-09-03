import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _test_dirs(test_patch: str) -> list[str]:
    paths = set()
    for match in re.finditer(
        r"^diff --git a/(.+?) b/(.+)$", test_patch or "", re.MULTILINE
    ):
        paths.add(match.group(2))
    return sorted({p.split("/")[0] + "/test" for p in paths if "/" in p})


class Ros2cliFoxyImageBase(Image):
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
        return "ros:foxy-ros-base"

    def image_tag(self) -> str:
        return "base-524_to_524"

    def workdir(self) -> str:
        return "base-524_to_524"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo

        apt = (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    ca-certificates \\\n"
            "    curl \\\n"
            "    build-essential \\\n"
            "    git \\\n"
            "    gnupg \\\n"
            "    make \\\n"
            "    python3 \\\n"
            "    python3-pip \\\n"
            "    python3-pytest \\\n"
            "    python3-pytest-cov \\\n"
            "    python3-pytest-timeout \\\n"
            "    python3-colcon-common-extensions \\\n"
            "    ros-foxy-example-interfaces \\\n"
            "    ros-foxy-launch-testing \\\n"
            "    ros-foxy-launch-testing-ros \\\n"
            "    ros-foxy-ros-testing \\\n"
            "    ros-foxy-test-msgs \\\n"
            "    sudo \\\n"
            "    wget \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

        sections = ["FROM " + self.dependency()]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(apt)
        sections.append('RUN git clone "${REPO_URL}" /home/' + repo)
        sections.append("WORKDIR /home/" + repo)
        sections.append("RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}")
        sections.append(Image._HARDENING_BLOCK.rstrip("\n"))
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class Ros2cliFoxyImageDefault(Image):
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
        return Ros2cliFoxyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env_block = """export CI=true
export PYTHONUNBUFFERED=1
export ROS_DOMAIN_ID=42
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_LOCALHOST_ONLY=1

. /opt/ros/foxy/setup.sh"""

        build_block = """colcon --log-base /home/ws/log build \\
    --base-paths /home/{pr.repo} \\
    --build-base /home/ws/build \\
    --install-base /home/ws/install \\
    --merge-install \\
    --symlink-install \\
    --cmake-args -DCMAKE_CXX_STANDARD=17 -DBUILD_TESTING=OFF > /dev/null 2>&1 || true
. /home/ws/install/setup.sh""".format(pr=self.pr)

        test_cmd = (
            "python3 -m pytest -v -rA --continue-on-collection-errors "
            "-p no:cacheprovider --timeout=900 --timeout-method=thread "
            + " ".join(_test_dirs(self.pr.test_patch))
        )

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || (git remote add origin https://github.com/{pr.org}/{pr.repo}.git 2>/dev/null; git fetch --depth=1 origin {pr.base.sha}; git remote remove origin)
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

mkdir -p /home/ws
. /opt/ros/foxy/setup.sh
colcon --log-base /home/ws/log build \\
    --base-paths /home/{pr.repo} \\
    --build-base /home/ws/build \\
    --install-base /home/ws/install \\
    --merge-install \\
    --symlink-install \\
    --cmake-args -DCMAKE_CXX_STANDARD=17 -DBUILD_TESTING=OFF

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
{env_block}

cd /home/{pr.repo}
{build_block}
{test_cmd}

""".format(
                    pr=self.pr,
                    env_block=env_block,
                    build_block=build_block,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
{env_block}

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{build_block}
{test_cmd}

""".format(
                    pr=self.pr,
                    env_block=env_block,
                    build_block=build_block,
                    test_cmd=test_cmd,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
{env_block}

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{build_block}
{test_cmd}

""".format(
                    pr=self.pr,
                    env_block=env_block,
                    build_block=build_block,
                    test_cmd=test_cmd,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{prepare_commands}

{self.clear_env}

"""


@Instance.register("ros2", "ros2cli_524_to_524")
class ROS2CLI_524_TO_524(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Ros2cliFoxyImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        node = r"[^\s:]+\.py(?:::[^\s]+)?"
        status_after = re.compile(
            rf"^({node})\s+(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
        )
        status_before = re.compile(
            rf"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)"
            rf"(?:\s+\[\s*\d+\s*\])?\s+({node})"
        )

        for raw_line in clean_log.splitlines():
            line = raw_line.strip()

            match = status_after.match(line)
            if match:
                name, status = match.group(1), match.group(2)
            else:
                match = status_before.match(line)
                if not match:
                    continue
                status, name = match.group(1), match.group(2)
                if "::" not in name and status != "ERROR":
                    continue

            name = name.rstrip(":")

            if status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
