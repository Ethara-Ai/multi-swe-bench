import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ReactorCoreEraBImageBase(Image):
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
        return "eclipse-temurin:11"

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git

{code}

{copy_commands}

{self.clear_env}

"""


class ReactorCoreEraBImageDefault(Image):
    """Multi-module reactor-core, Gradle 3.x-6.x on JDK 11.

    40 PRs using old-style build.gradle (no version catalogs).
    Patches: jcenter->mavenCentral, remove dead Spring repos,
    disable artifactory/gradle-enterprise/jcstress plugins,
    strip jsr166 backport.
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

    def dependency(self) -> Optional[Image]:
        return ReactorCoreEraBImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _gradle_jvmargs(self) -> str:
        return (
            'sed -i "/^org.gradle.jvmargs=/d" gradle.properties 2>/dev/null || true\n'
            'echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties'
        )

    def files(self) -> list[File]:
        gradle_jvmargs = self._gradle_jvmargs()

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
{gradle_jvmargs}

# Replace dead repos in all build.gradle files
find . -name "build.gradle" -exec sed -i "s|jcenter()|mavenCentral()|g" {{}} \\;
find . -name "build.gradle" -exec sed -i "/repo\\.spring\\.io/d" {{}} \\;

# Disable artifactory plugin (publishing only, not needed for tests)
sed -i 's|id "com.jfrog.artifactory".*|//id "com.jfrog.artifactory"|' build.gradle || true
if [ -f gradle/setup.gradle ]; then
    sed -i 's|apply plugin: "com.jfrog.artifactory"|//apply plugin: "com.jfrog.artifactory"|' gradle/setup.gradle || true
    sed -i '/artifactoryPublish {{/,/^}}/s|^|//|' gradle/setup.gradle || true
fi

# Disable Gradle Enterprise plugin (phones home)
sed -i 's|id "com.gradle.enterprise".*|//id "com.gradle.enterprise"|g' settings.gradle || true
sed -i 's|id "com.gradle.build-scan".*|//id "com.gradle.build-scan"|g' settings.gradle || true
sed -i '/gradleEnterprise {{/,/^}}/s|^|//|' settings.gradle || true

# Strip jsr166 backport (JDK 11 has java.util.concurrent.Flow natively)
find . -name "build.gradle" -exec sed -i '/jsr166backport/d' {{}} \\;
find . -name "build.gradle" -exec sed -i '/Xbootclasspath/d' {{}} \\;
find . -name "build.gradle" -exec sed -i '/extendsFrom.*jsr166backport/d' {{}} \\;

# Disable jcstress plugin (concurrency stress tests, not needed for regular tests)
sed -i 's|classpath.*jcstress-gradle-plugin.*|//&|' build.gradle || true
find . -name "build.gradle" -exec sed -i 's|apply plugin: "jcstress"|//apply plugin: "jcstress"|' {{}} \\;
find . -name "build.gradle" -exec sed -i '/jcstress {{/,/^}}/s|^|//|' {{}} \\;
find . -name "build.gradle" -exec sed -i '/jcstressJar {{/,/^}}/s|^|//|' {{}} \\;
find . -name "build.gradle" -exec sed -i '/jcstressImplementation/d' {{}} \\;
find . -name "build.gradle" -exec sed -i '/jcstressAnnotationProcessor/d' {{}} \\;

# Set up init.gradle for reliable dependency resolution
mkdir -p ~/.gradle && cat <<'INITGRADLE' > ~/.gradle/init.gradle
allprojects {{
    buildscript {{
        repositories {{
            maven {{ url 'https://maven.aliyun.com/repository/public/' }}
            maven {{ url 'https://maven.aliyun.com/repository/jcenter/' }}
            maven {{ url 'https://maven.aliyun.com/repository/google/' }}
            maven {{ url 'https://plugins.gradle.org/m2/' }}
            mavenCentral()
        }}
    }}

    repositories {{
        maven {{ url 'https://maven.aliyun.com/repository/public/' }}
        maven {{ url 'https://maven.aliyun.com/repository/jcenter/' }}
        maven {{ url 'https://maven.aliyun.com/repository/google/' }}
        mavenCentral()
    }}
}}
INITGRADLE

# Pre-warm: resolve dependencies and compile
./gradlew :reactor-core:test --init-script ~/.gradle/init.gradle --no-daemon --max-workers 2 --continue || true
""".format(pr=self.pr, gradle_jvmargs=gradle_jvmargs),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
{gradle_jvmargs}
./gradlew :reactor-core:test --init-script ~/.gradle/init.gradle --no-daemon --max-workers 2 --continue
""".format(pr=self.pr, gradle_jvmargs=gradle_jvmargs),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard HEAD && git apply --whitespace=nowarn --reject /home/test.patch || true
{gradle_jvmargs}
./gradlew :reactor-core:test --init-script ~/.gradle/init.gradle --no-daemon --max-workers 2 --continue

""".format(pr=self.pr, gradle_jvmargs=gradle_jvmargs),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard HEAD && git apply --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
{gradle_jvmargs}
./gradlew :reactor-core:test --init-script ~/.gradle/init.gradle --no-daemon --max-workers 2 --continue

""".format(pr=self.pr, gradle_jvmargs=gradle_jvmargs),
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
        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("reactor", "reactor_core_era_b")
class ReactorCoreEraB(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReactorCoreEraBImageDefault(self.pr, self._config)

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
            re.compile(r"^:(\S+)$"),
            re.compile(r"^:(\S+) UP-TO-DATE$"),
            re.compile(r"^:(\S+) FROM-CACHE$"),
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^:(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^> Task :(\S+) NO-SOURCE$"),
            re.compile(r"^:(\S+) SKIPPED$"),
            re.compile(r"^:(\S+) NO-SOURCE$"),
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
