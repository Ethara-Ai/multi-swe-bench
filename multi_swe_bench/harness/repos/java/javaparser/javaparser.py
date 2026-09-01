import re
import textwrap

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class JavaParserImageBase(Image):
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
ENV CI=true
ENV MAVEN_OPTS="-Xmx4g -XX:+ExitOnOutOfMemoryError"
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-11-jdk maven \
    && ln -sfn "$(dirname $(dirname $(readlink -f $(which javac))))" /usr/lib/jvm/default-java
ENV JAVA_HOME=/usr/lib/jvm/default-java

{code}

{self.clear_env}

"""


class JavaParserImageDefault(Image):
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
        return JavaParserImageBase(self.pr, self._config)

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
export CI=true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git remote get-url origin >/dev/null 2>&1 || git remote add origin https://github.com/{pr.org}/{pr.repo}.git
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch --depth=1 origin {pr.base.sha}
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
if [ ! -f ~/.m2/settings.xml ]; then
    mkdir -p ~/.m2 && cat <<EOF > ~/.m2/settings.xml
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">

    <mirrors>
        <mirror>
            <id>aliyunmaven</id>
            <mirrorOf>central</mirrorOf>
            <name>Aliyun Maven Mirror</name>
            <url>https://maven.aliyun.com/repository/public</url>
        </mirror>
    </mirrors>

</settings>
EOF
else
  grep -q "<mirror>" ~/.m2/settings.xml || sed -i '/<\\/settings>/i \\
  <mirrors> \\
      <mirror> \\
          <id>aliyunmaven</id> \\
          <mirrorOf>central</mirrorOf> \\
          <name>Aliyun Maven Mirror</name> \\
          <url>https://maven.aliyun.com/repository/public</url> \\
      </mirror> \\
  </mirrors>' ~/.m2/settings.xml
fi
if [ -f ./mvnw ]; then
    ./mvnw -B -q dependency:go-offline -fae || true
    ./mvnw -B -q test-compile -fae || true
else
    mvn -B -q dependency:go-offline -fae || true
    mvn -B -q test-compile -fae || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ -f ./mvnw ]; then ./mvnw -B clean test -fae; else mvn -B clean test -fae; fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f ./mvnw ]; then ./mvnw -B clean test -fae; else mvn -B clean test -fae; fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f ./mvnw ]; then ./mvnw -B clean test -fae; else mvn -B clean test -fae; fi

""".format(pr=self.pr),
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
            # Extract proxy host and port
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


# Era coverage: PRs #2522..#3034 (span 512, ~2020-08 to ~2021-06).
# Verified across all 5 dataset base SHAs (4b2858cc, cac75a4c, bdf9ac04,
# f20d6fdc, b75521515b93): pom.xml pins <java.version>1.8</java.version>
# with Maven and maven-surefire-plugin throughout. JDK 11 in the image
# compiles Java 8 source cleanly. Single-era configuration is justified.
@Instance.register("javaparser", "javaparser")
class JavaParser(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JavaParserImageDefault(self.pr, self._config)

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
        re_ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        re_running = re.compile(r"\[INFO\] Running (\S+)")
        re_perclass = re.compile(
            r"\[(?:INFO|ERROR|WARNING)\] Tests run: (\d+), Failures: (\d+), "
            r"Errors: (\d+), Skipped: (\d+),\s+Time elapsed:.* - in (\S+)"
        )
        re_compile_err = re.compile(
            r"[\w./]+/([A-Z]\w+)\.java:\[\d+,\d+\] cannot find symbol"
        )
        re_fork_crash = re.compile(
            r"Crashed tests:\s*\n\s*\[ERROR\]\s+([\w.$]+)"
        )

        text = re_ansi.sub("", test_log)

        passed_tests = set()
        skipped_tests = set()
        failed_tests = set()
        summarized = set()

        for line in text.split("\n"):
            m = re_perclass.search(line)
            if not m:
                continue
            tests_run = int(m.group(1))
            failures = int(m.group(2))
            errors = int(m.group(3))
            skipped = int(m.group(4))
            cls = m.group(5)
            summarized.add(cls)
            if failures > 0 or errors > 0:
                failed_tests.add(cls)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(cls)
            else:
                passed_tests.add(cls)
                for i in range(skipped):
                    skipped_tests.add(f"{cls}#skipped-{i}")

        for cls in {m.group(1) for m in re_running.finditer(text)} - summarized:
            failed_tests.add(cls)
            passed_tests.discard(cls)
        for m in re_fork_crash.finditer(text):
            failed_tests.add(m.group(1))
            passed_tests.discard(m.group(1))
        for m in re_compile_err.finditer(text):
            failed_tests.add(f"<compile-error>{m.group(1)}")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
