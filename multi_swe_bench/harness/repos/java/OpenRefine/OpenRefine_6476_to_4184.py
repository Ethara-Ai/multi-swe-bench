import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenRefineImageBaseJDK17(Image):
    """Base image for OpenRefine PRs #4184-#6476 (JDK 17 era).

    CI tested on JDK [8,14] → [11] → [11,17] → [11,21] across this range.
    java.minversion=11 throughout (source=1.8 for earliest PRs).
    JDK 17 is backwards compatible with all of these.
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
        return "ubuntu:22.04"

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

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && \\
    apt-get install -y git openjdk-17-jdk maven && \\
    rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class OpenRefineImageDefaultJDK17(Image):
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
        return OpenRefineImageBaseJDK17(self.pr, self._config)

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
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}
mvn clean test -fn || true
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
mvn clean test -fn
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
mvn clean test -fn
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn clean test -fn
""".format(repo=self.pr.repo),
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
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )
                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
                """
                )

        return f"""FROM {name}:{tag}
{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("OpenRefine", "OpenRefine_6476_to_4184")
class OPENREFINE_6476_TO_4184(Instance):
    """OpenRefine PRs #4184-#6476: JDK 17 era.

    Covers PRs created 2021-09 to 2024-03.
    CI tested on JDK [8,14] → [11,17] → [11,21].
    JDK 17 is backwards compatible with java.minversion=11 / source=1.8.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenRefineImageDefaultJDK17(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = r"(?:\x1B\[[0-9;]*m)?"

        re_pass_tests = [
            re.compile(
                rf"\[?{ansi_escape}INFO{ansi_escape}\]?\s+.*?Tests run:.*?[-]+ in {ansi_escape}([a-zA-Z0-9_.]+){ansi_escape}"
            ),
            re.compile(
                r"(?:\[\x1B\[[0-9;]*m)?INFO(?:\x1B\[[0-9;]*m)?\]?\s+([a-zA-Z0-9 \-_.]+?)\s+\.{3,}\s+(?:\x1B\[[0-9;]*m)?SUCCESS"
            ),
        ]

        re_fail_tests = [
            re.compile(
                rf"\[?{ansi_escape}ERROR{ansi_escape}\]?\s+.*?Tests run:.*?[-]+ in {ansi_escape}([a-zA-Z0-9_.]+){ansi_escape}"
            ),
            re.compile(
                r"(?:\[\x1B\[[0-9;]*m)?INFO(?:\x1B\[[0-9;]*m)?\]?\s+([a-zA-Z0-9 \-_.]+?)\s+\.{3,}\s+(?:\x1B\[[0-9;]*m)?FAILURE"
            ),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue
            for pattern in re_pass_tests:
                pass_match = pattern.match(line)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    break
            for pattern in re_fail_tests:
                fail_match = pattern.match(line)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    break

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
