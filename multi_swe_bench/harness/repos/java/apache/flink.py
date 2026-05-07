import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FlinkImageBase(Image):
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

        global_env = self.global_env
        global_env_block = f"\n{global_env}\n" if global_env.strip() else ""

        return f"""FROM {image_name}
{global_env_block}ENV LC_ALL=C.UTF-8 \\
    TZ=UTC

WORKDIR /home/

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        git \\
        ca-certificates \\
        wget \\
        openjdk-8-jdk && \\
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        openjdk-17-jdk && \\
    rm -rf /var/lib/apt/lists/*

RUN wget -q https://archive.apache.org/dist/maven/maven-3/3.8.6/binaries/apache-maven-3.8.6-bin.tar.gz -O /tmp/maven.tar.gz && \\
    tar xzf /tmp/maven.tar.gz -C /opt && \\
    ln -sf /opt/apache-maven-3.8.6/bin/mvn /usr/local/bin/mvn && \\
    rm /tmp/maven.tar.gz

{code}

{self.clear_env}

"""

    # PRs that need JDK 17 (JDK 11 target PRs also use JDK 17 since 17 is backward-compatible)
    # All other PRs (245 of 285) use JDK 8
    _NEEDS_JDK17 = {
        19970, 22385, 23439, 23899, 24247, 24375, 24444, 24930,
        25504, 25511, 25576, 25712, 26050, 26139, 26196, 26365,
        26487, 26513, 26535, 26546, 26684, 26717, 26763, 26790,
        26807, 26935, 27046, 27193, 27207, 27215, 27285, 27336,
        27337, 27363, 27435, 27443, 27481, 27645, 27646, 27761,
    }

    @classmethod
    def _extra_prepare(cls, pr_number: int) -> str:
        return ""

    @classmethod
    def _env_prefix(cls, pr_number: int) -> str:
        parts = []
        if pr_number in cls._NEEDS_JDK17:
            parts.append(
                'export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))\n'
                'JDK17_HOME=$(ls -d /usr/lib/jvm/java-17-openjdk-* 2>/dev/null | head -1)\n'
                'if [ -n "$JDK17_HOME" ]; then export JAVA_HOME="$JDK17_HOME"; fi\n'
                'export PATH="$JAVA_HOME/bin:$PATH"'
            )
        else:
            parts.append(
                'export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))\n'
                'JDK8_HOME=$(ls -d /usr/lib/jvm/java-8-openjdk-* 2>/dev/null | head -1)\n'
                'if [ -n "$JDK8_HOME" ]; then export JAVA_HOME="$JDK8_HOME"; fi\n'
                'export PATH="$JAVA_HOME/bin:$PATH"'
            )
        return "\n".join(parts)


class FlinkImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return FlinkImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env_prefix = FlinkImageBase._env_prefix(self.pr.number)

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

cd /home/flink
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{env_prefix}
export MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"
mvn clean install -DskipTests -Denforcer.skip=true -Dfast -Pskip-webui-build -T 1C || true
""".format(pr=self.pr, env_prefix=env_prefix),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

{env_prefix}
export MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"
cd /home/flink
mvn clean test -fn -Denforcer.skip=true -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false || true
""".format(pr=self.pr, env_prefix=env_prefix),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

{env_prefix}
export MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"
cd /home/flink
git apply --whitespace=nowarn /home/test.patch || true
mvn clean test -fn -Denforcer.skip=true -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false || true

""".format(pr=self.pr, env_prefix=env_prefix),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

{env_prefix}
export MAVEN_OPTS="-Xmx4g -XX:+UseG1GC"
cd /home/flink
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
mvn clean test -fn -Denforcer.skip=true -Dsurefire.useFile=false -Dmaven.test.skip=false -DfailIfNoTests=false || true

""".format(pr=self.pr, env_prefix=env_prefix),
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


@Instance.register("apache", "flink")
class Flink(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlinkImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+), Time elapsed: [\d.]+ .+? in (.+)"
        )

        for line in test_log.splitlines():
            match = pattern.search(line)
            if match:
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5)

                if (
                    tests_run > 0
                    and failures == 0
                    and errors == 0
                    and skipped != tests_run
                ):
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        # Dedup: a test class may appear in multiple modules with different results
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
