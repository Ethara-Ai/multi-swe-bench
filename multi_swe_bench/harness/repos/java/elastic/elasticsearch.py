import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _get_major_minor(pr: PullRequest) -> tuple[int, int]:
    match = re.match(r"^(\d+)\.(\d+)", pr.base.ref)
    if match:
        return int(match.group(1)), int(match.group(2))
    return 9, 0


def _needs_zulu(pr: PullRequest) -> bool:
    major, minor = _get_major_minor(pr)
    return major == 7 and minor < 17


# --- Discovered JDK requirements from .ci/java-versions.properties ---
# 7.6-7.8:  ES_BUILD_JAVA=openjdk13  (Gradle 6.2)
# 7.9-7.11: ES_BUILD_JAVA=openjdk14  (Gradle 6.5-6.6)
# 7.12-7.13: ES_BUILD_JAVA=openjdk15 (Gradle 6.8-7.0)
# 7.14-7.16: ES_BUILD_JAVA=openjdk16 (Gradle 7.1-7.3)
# 7.17:     ES_BUILD_JAVA=openjdk17  (Gradle 8.10)
# 8.x:      ES_BUILD_JAVA=openjdk17  (Gradle 7.3-9.x)
# 9.x:      ES_BUILD_JAVA=openjdk21  (Gradle 8.14-9.x)
#
# JDK 13/14/15/16 are NOT in Ubuntu 22.04 apt repos.
# Azul Zulu repos provide zulu15-jdk and zulu16-jdk.
# For JDK 13/14 (7.6-7.11): install via Zulu repos; availability
# varies by architecture (amd64 has more packages than arm64).
#
# Elasticsearch CANNOT run tests as root:
#   java.lang.RuntimeException: can not run elasticsearch as root
# All scripts must execute as non-root user 'esuser'.


_JAVA_HOME_DETECT = (
    'export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))\n'
    'export PATH=$JAVA_HOME/bin:$PATH'
)


class ElasticsearchImageBaseZulu(Image):
    """Base image with Azul Zulu repos for ES 7.6-7.16 (JDK 13-16)."""

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
        return "base-zulu"

    def workdir(self) -> str:
        return "base-zulu"

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

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai -Xmx48g"
ENV GRADLE_OPTS="-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y git curl gnupg ca-certificates
RUN curl -s https://repos.azul.com/azul-repo.key | gpg --dearmor -o /usr/share/keyrings/azul.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/azul.gpg] https://repos.azul.com/zulu/deb stable main" | tee /etc/apt/sources.list.d/zulu.list \\
    && apt-get update

{code}

{self.clear_env}

"""


class ElasticsearchImageBaseJDK17(Image):
    """Base image with JDK 17 for ES 7.17 and 8.x."""

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
        return "base-jdk-17"

    def workdir(self) -> str:
        return "base-jdk-17"

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

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai -Xmx48g"
ENV GRADLE_OPTS="-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y git openjdk-17-jdk curl

{code}

{self.clear_env}

"""


class ElasticsearchImageBaseJDK21(Image):
    """Base image with JDK 21 for ES 9.x."""

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
        return "base-jdk-21"

    def workdir(self) -> str:
        return "base-jdk-21"

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

ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai -Xmx48g"
ENV GRADLE_OPTS="-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200"
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

WORKDIR /home/

RUN apt-get update && apt-get install -y git openjdk-21-jdk curl

{code}

{self.clear_env}

"""


class ElasticsearchImageDefault(Image):
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
        if _needs_zulu(self.pr):
            return ElasticsearchImageBaseZulu(self.pr, self._config)
        major, _ = _get_major_minor(self.pr)
        if major <= 8:
            return ElasticsearchImageBaseJDK17(self.pr, self._config)
        return ElasticsearchImageBaseJDK21(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _make_install_jdk_sh(self) -> str:
        return r"""#!/bin/bash
set -e
cd /home/{repo}
JAVA_VER=$(grep ES_BUILD_JAVA .ci/java-versions.properties | head -1 | sed 's/.*openjdk//')
apt-get install -y "zulu${JAVA_VER}-jdk" || apt-get install -y "zulu${JAVA_VER}-ca-jdk" || true
""".replace("{repo}", self.pr.repo)

    def files(self) -> list[File]:
        base_files = [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
        ]

        if _needs_zulu(self.pr):
            base_files.append(
                File(".", "install-jdk.sh", self._make_install_jdk_sh())
            )
            base_files.append(
                File(
                    ".",
                    "prepare.sh",
                    """#!/bin/bash
set -e

{java_home}

cd /home/{pr.repo}
bash /home/check_git_changes.sh

# Inject heap + parallel settings into gradle.properties
sed -i 's/^org.gradle.jvmargs=.*/org.gradle.jvmargs=-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+HeapDumpOnOutOfMemoryError -Xss2m --add-exports jdk.compiler\/com.sun.tools.javac.util=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.api=ALL-UNNAMED --add-opens java.base\/java.time=ALL-UNNAMED/' gradle.properties
grep -q "org.gradle.workers.max" gradle.properties || echo "org.gradle.workers.max=16" >> gradle.properties

./gradlew clean classes testClasses --continue || true

""".format(pr=self.pr, java_home=_JAVA_HOME_DETECT),
                ),
            )
        else:
            base_files.append(
                File(
                    ".",
                    "prepare.sh",
                    """#!/bin/bash
set -e

{java_home}

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Inject heap + parallel settings into gradle.properties
sed -i 's/^org.gradle.jvmargs=.*/org.gradle.jvmargs=-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+HeapDumpOnOutOfMemoryError -Xss2m --add-exports jdk.compiler\/com.sun.tools.javac.util=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.api=ALL-UNNAMED --add-opens java.base\/java.time=ALL-UNNAMED/' gradle.properties
grep -q "org.gradle.workers.max" gradle.properties || echo "org.gradle.workers.max=16" >> gradle.properties

./gradlew clean classes testClasses --continue || true

""".format(pr=self.pr, java_home=_JAVA_HOME_DETECT),
                ),
            )

        base_files.extend(
            [
                File(
                    ".",
                    "run.sh",
                    """#!/bin/bash
set -e

{java_home}

cd /home/{pr.repo}
sed -i 's/^org.gradle.jvmargs=.*/org.gradle.jvmargs=-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+HeapDumpOnOutOfMemoryError -Xss2m --add-exports jdk.compiler\/com.sun.tools.javac.util=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.api=ALL-UNNAMED --add-opens java.base\/java.time=ALL-UNNAMED/' gradle.properties
grep -q "org.gradle.workers.max" gradle.properties || echo "org.gradle.workers.max=16" >> gradle.properties
./gradlew clean test --continue

""".format(pr=self.pr, java_home=_JAVA_HOME_DETECT),
                ),
                File(
                    ".",
                    "test-run.sh",
                    """#!/bin/bash
set -e

{java_home}

cd /home/{pr.repo}
sed -i 's/^org.gradle.jvmargs=.*/org.gradle.jvmargs=-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+HeapDumpOnOutOfMemoryError -Xss2m --add-exports jdk.compiler\/com.sun.tools.javac.util=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.api=ALL-UNNAMED --add-opens java.base\/java.time=ALL-UNNAMED/' gradle.properties
grep -q "org.gradle.workers.max" gradle.properties || echo "org.gradle.workers.max=16" >> gradle.properties
git apply /home/test.patch
./gradlew clean test --continue

""".format(pr=self.pr, java_home=_JAVA_HOME_DETECT),
                ),
                File(
                    ".",
                    "fix-run.sh",
                    """#!/bin/bash
set -e

{java_home}

cd /home/{pr.repo}
sed -i 's/^org.gradle.jvmargs=.*/org.gradle.jvmargs=-Xmx48g -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -XX:+HeapDumpOnOutOfMemoryError -Xss2m --add-exports jdk.compiler\/com.sun.tools.javac.util=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler\/com.sun.tools.javac.api=ALL-UNNAMED --add-opens java.base\/java.time=ALL-UNNAMED/' gradle.properties
grep -q "org.gradle.workers.max" gradle.properties || echo "org.gradle.workers.max=16" >> gradle.properties
git apply /home/test.patch /home/fix.patch
./gradlew clean test --continue

""".format(pr=self.pr, java_home=_JAVA_HOME_DETECT),
                ),
            ]
        )

        return base_files

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

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
                    RUN mkdir -p ~/.gradle && \\
                        if [ ! -f "$HOME/.gradle/gradle.properties" ]; then \\
                            touch "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        if ! grep -q "systemProp.http.proxyHost" "$HOME/.gradle/gradle.properties"; then \\
                            echo 'systemProp.http.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.http.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyHost={proxy_host}' >> "$HOME/.gradle/gradle.properties" && \\
                            echo 'systemProp.https.proxyPort={proxy_port}' >> "$HOME/.gradle/gradle.properties"; \\
                        fi && \\
                        echo 'export GRADLE_USER_HOME=/root/.gradle' >> ~/.bashrc && \\
                        /bin/bash -c "source ~/.bashrc"
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f ~/.gradle/gradle.properties
                """
                )

        if _needs_zulu(self.pr):
            zulu_install = textwrap.dedent(
                f"""
                RUN cd /home/{self.pr.repo} && \\
                    git reset --hard && \\
                    git checkout {self.pr.base.sha} && \\
                    bash /home/install-jdk.sh
                """
            )
            user_setup = textwrap.dedent(
                """
                RUN useradd -m -s /bin/bash esuser && chown -R esuser:esuser /home/

                USER esuser
                """
            )
            prepare_commands = "RUN bash /home/prepare.sh"
        else:
            zulu_install = ""
            user_setup = textwrap.dedent(
                """
                RUN useradd -m -s /bin/bash esuser && chown -R esuser:esuser /home/

                USER esuser
                """
            )
            prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{zulu_install}

{user_setup}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("elastic", "elasticsearch")
class Elasticsearch(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ElasticsearchImageDefault(self.pr, self._config)

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

        passed_res = [
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        for line in test_log.splitlines():
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.match(line)
                if m:
                    skipped_tests.add(m.group(1))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
