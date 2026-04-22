import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Test command targeting only the subprojects whose tests actually compile and run.
# Global `sbt test` fails because:
#   1. com_equella_core requires npm for JS compilation (package-lock.json out of sync)
#   2. BIRT plugin depends on defunct repository.springsource.com
# These 4 subprojects produce 137 JUnit tests total, all via junit-interface 0.11.
_SBT_TEST_CMD = (
    'sbt "com_tle_platform_common/test" "com_equella_base/test"'
    ' "com_tle_web_sections/test" "com_equella_serverbase/test"'
)


class openEQUELLAImageBase(Image):
    """Base image for openEQUELLA.

    Uses eclipse-temurin:8-jdk because SBT 1.1.6 + Scala 2.12.x crash on JDK 17
    (java.io.IOError: /packages cannot be represented as URI).
    SBT 1.1.6 is installed manually from GitHub releases.
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

    def dependency(self) -> Union[str, "Image"]:
        return "eclipse-temurin:8-jdk"

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && \\
    apt-get install -y git curl && \\
    rm -rf /var/lib/apt/lists/*

RUN curl -sL https://github.com/sbt/sbt/releases/download/v1.1.6/sbt-1.1.6.tgz | tar xz -C /opt && \\
    ln -s /opt/sbt/bin/sbt /usr/local/bin/sbt

{code}

{self.clear_env}

"""


class openEQUELLAImageDefault(Image):
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
        return openEQUELLAImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
{test_cmd} || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, test_cmd=_SBT_TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=_SBT_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=_SBT_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(repo=self.pr.repo, test_cmd=_SBT_TEST_CMD),
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
        proxy_setup = ""
        proxy_cleanup = ""
        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break
            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    ENV SBT_OPTS="-Dhttps.proxyHost={proxy_host} -Dhttps.proxyPort={proxy_port} -Dhttp.proxyHost={proxy_host} -Dhttp.proxyPort={proxy_port}"
                """
                )
                proxy_cleanup = textwrap.dedent(
                    """
                    ENV SBT_OPTS=""
                """
                )

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{proxy_setup}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("openequella", "openEQUELLA")
class openEQUELLA(Instance):
    """openEQUELLA - Java/Scala LMS built with SBT 1.1.6.

    Both PRs (#2344, #2912) use identical toolchain:
        SBT 1.1.6, Scala 2.12.x, JDK 8, junit-interface 0.11.
    No era split needed.

    Test output is parsed from SBT junit-interface format:
        [error] Test <fqn> failed: <message>, took X sec   (failed)
        [debug] Test <fqn> finished, took X sec             (finished, both pass+fail)
    Passed = finished - failed.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return openEQUELLAImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # junit-interface 0.11 output patterns (Docker-verified):
        #   [error] Test com.test.FailTest.testError failed: RuntimeException: boom, took 0.005 sec
        #   [debug] Test com.dytech.devlib.PropBagExTest.testGetIntNode finished, took 0.039 sec
        # Both passed and failed tests produce "finished" lines.
        # Only failed tests produce "failed" lines at [error] level.
        ansi_escape = re.compile(r"\x1B\[[0-9;]*m")
        failed_re = re.compile(r"^\[error\] Test (\S+) failed:")
        finished_re = re.compile(r"^\[debug\] Test (\S+) finished,")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = failed_re.match(line)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = finished_re.match(line)
            if m:
                passed_tests.add(m.group(1))

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
