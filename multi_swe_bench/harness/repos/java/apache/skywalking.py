import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MVN_SKIPS = (
    "-Dcheckstyle.skip=true "
    "-Drat.skip=true "
    "-Dlicense.skip=true "
    "-Denforcer.skip=true "
    "-Danimal.sniffer.skip=true "
    "-Dmdep.analyze.skip=true "
    "-Dmaven.javadoc.skip=true "
    "-Dgpg.skip=true"
)

_MVN_TEST = (
    "clean test -B -fn "
    f"{_MVN_SKIPS} "
    "-Dsurefire.useFile=false -Dsurefire.skipAfterFailureCount=0 "
    "-DfailIfNoTests=false"
)

_MVN_WARMUP = f"clean install -T 4 -B -q -fn -DskipTests {_MVN_SKIPS}"

_JAVA_ENV = """export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS='-Xmx4g -XX:+UseParallelGC'"""

_MVN_SETUP = """if [ -x ./mvnw ]; then
  MVN="./mvnw"
else
  MVN="mvn"
fi"""

_SUBMODULE_SETUP = """if [ -f .gitmodules ]; then
  sed -i 's|git@github.com:|https://github.com/|g' .gitmodules
  git submodule update --init --recursive || true
fi"""

_SUREFIRE_DUMP = """echo "===== SUREFIRE REPORTS BEGIN ====="
find . -path '*/target/surefire-reports/TEST-*.xml' -exec cat {} \\; 2>/dev/null
echo "===== SUREFIRE REPORTS END =====\""""

class SkywalkingImageBase(Image):
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

ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-8-jdk \
    && apt-get install -y --no-install-recommends \
    git ca-certificates curl unzip make maven \
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SkywalkingImageDefault(Image):
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
        return SkywalkingImageBase(self.pr, self._config)

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

{java_env}

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{org}/{repo}.git {sha}
    git checkout --detach FETCH_HEAD
else
    git checkout --detach {sha}
fi
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

{submodule_setup}

{mvn_setup}

$MVN {mvn_warmup} || true
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    java_env=_JAVA_ENV,
                    submodule_setup=_SUBMODULE_SETUP,
                    mvn_setup=_MVN_SETUP,
                    mvn_warmup=_MVN_WARMUP,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

set +e
$MVN {mvn_test}
MVN_STATUS=$?
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_JAVA_ENV,
                    mvn_setup=_MVN_SETUP,
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch

set +e
$MVN {mvn_test}
MVN_STATUS=$?
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_JAVA_ENV,
                    mvn_setup=_MVN_SETUP,
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{java_env}

cd /home/{pr.repo}
{mvn_setup}

git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.class' /home/test.patch /home/fix.patch

set +e
$MVN {mvn_test}
MVN_STATUS=$?
set -e

{surefire_dump}

exit $MVN_STATUS
""".format(
                    pr=self.pr,
                    java_env=_JAVA_ENV,
                    mvn_setup=_MVN_SETUP,
                    mvn_test=_MVN_TEST,
                    surefire_dump=_SUREFIRE_DUMP,
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


@Instance.register("apache", "skywalking")
class Skywalking(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SkywalkingImageDefault(self.pr, self._config)

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

        def record(test_name, tests_run, failures, errors, skipped):
            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(test_name)
            elif tests_run > 0:
                passed_tests.add(test_name)

        def finish():
            passed_tests.difference_update(failed_tests)
            skipped_tests.difference_update(failed_tests)
            skipped_tests.difference_update(passed_tests)
            return TestResult(
                passed_count=len(passed_tests),
                failed_count=len(failed_tests),
                skipped_count=len(skipped_tests),
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        re_case = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
        re_attr = re.compile(r'(\w+)="([^"]*)"')
        saw_xml = False
        for match in re_case.finditer(test_log):
            attrs = dict(re_attr.findall(match.group(1)))
            method = attrs.get("name", "")
            if not method:
                continue
            saw_xml = True
            classname = attrs.get("classname", "")
            name = f"{classname}#{method}" if classname else method
            body = match.group(3) or ""
            if "<failure" in body or "<error" in body:
                failed_tests.add(name)
            elif "<skipped" in body:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        if saw_xml:
            return finish()

        re_running = re.compile(r"Running\s+([\w.$]+)\s*$")
        re_summary_with_class = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
            r"\s*Skipped:\s*(\d+),\s*Time elapsed:.*?(?:--|-)\s+in\s+([\w.$]+)"
        )
        re_summary_plain = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
            r"\s*Skipped:\s*(\d+),\s*Time elapsed:"
        )

        current_class = None
        for line in test_log.splitlines():
            match = re_summary_with_class.search(line)
            if match:
                current_class = None
                record(
                    match.group(5),
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                    int(match.group(4)),
                )
                continue

            match = re_summary_plain.search(line)
            if match:
                if current_class:
                    record(
                        current_class,
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        int(match.group(4)),
                    )
                    current_class = None
                continue

            match = re_running.search(line)
            if match:
                current_class = match.group(1)

        return finish()
