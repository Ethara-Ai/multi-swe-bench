import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# wpilibsuite/RobotBuilder PR #402 ("Only allow values >= 0 for timeout").
# A Java Swing desktop app (FRC robot-code generator), built with **Gradle 5.4.1**
# (gradle wrapper), tests with **JUnit 4.12** via `./gradlew test`.
#
# Gradle 5.4.1 supports Java 8-12 -> base is ubuntu:22.04 + openjdk-11-jdk.
#
# The original build.gradle is UNBUILDABLE today (its publishing/versioning
# plugins pull grgit-core:3.0.0 from dead jcenter), so prepare.sh swaps in a
# minimal equivalent (MINIMAL_BUILD_GRADLE) that keeps the same compile+test
# classpath (all on Maven Central) and also handles the two Gradle gotchas:
#  1. test logging: the repo only logged "failed"; the minimal file enables
#     passed/failed/skipped so parse_log can see the passes.
#  2. Swing app -> `-Djava.awt.headless=true`; `ignoreFailures = true` keeps a
#     failing test from aborting the gradle build (we grade from printed results).

REPO_DIR = "RobotBuilder"

# The repo's build.gradle (2019) applies the WPILib versioning + repositories +
# jfrog-artifactory plugins, whose transitive dep `org.ajoberstar.grgit:grgit-core:3.0.0`
# lived ONLY on jcenter/bintray (shut down 2021) and is now unfetchable from any
# public repo (verified: not on Maven Central / jfrog / jitpack / frcmaven). Those
# plugins are only for publishing/version-stamping, NOT for `./gradlew test`, and
# every real project dependency (velocity/snakeyaml/commons/slf4j/junit/lombok) is
# on Maven Central. So prepare.sh replaces build.gradle with this minimal, buildable
# equivalent (same compile+test classpath) that also folds in the test logging
# (repo's build.gradle only logged "failed", which would hide passes from parse_log),
# headless Swing, and ignoreFailures.
MINIMAL_BUILD_GRADLE = """\
plugins {
    id 'java'
}

repositories {
    mavenCentral()
}

dependencies {
    compile 'org.apache.velocity:velocity-engine-core:2.1'
    compile 'org.yaml:snakeyaml:1.25'
    compile 'commons-io:commons-io:2.6'
    compile 'org.apache.commons:commons-lang3:3.9'
    compile 'org.slf4j:slf4j-api:1.7.28'
    compile 'org.slf4j:slf4j-jdk14:1.7.28'
    compile 'com.sun.activation:javax.activation:1.2.0'
    testCompile 'junit:junit:4.12'
    annotationProcessor 'org.projectlombok:lombok:1.18.8'
    compileOnly 'org.projectlombok:lombok:1.18.8'
}

compileJava {
    options.encoding = 'UTF-8'
}
compileTestJava {
    options.encoding = 'UTF-8'
}

test {
    testLogging {
        events 'passed', 'failed', 'skipped'
        exceptionFormat 'full'
    }
    // NOT headless: the tests construct a real Swing MainFrame (JFrame), which
    // throws HeadlessException under -Djava.awt.headless=true. The run scripts
    // provide a virtual X display via xvfb-run instead.
    //
    // FRC_HOME: RobotBuilder reads it via System.getenv; MainFrame -> Palette ->
    // Extensions.scanForComponents does `extensionsFolder.listFiles()` which NPEs
    // if the folder is missing. Point FRC_HOME at a pre-created dir (prepare.sh
    // makes $FRC_HOME/Robotbuilder/extensions) so listFiles() returns empty, not null.
    environment 'FRC_HOME', '/root/frchome'
    // forkEvery=0 + maxParallelForks=1: run the (scoped) suite in ONE JVM, single
    // threaded. Extensions is a Lombok @UtilityClass (static state); the real app
    // calls Extensions.init() in RobotBuilder.main() before the GUI boots. Unit
    // tests bypass main(), so a class that calls MainFrame.getInstance() without a
    // prior Extensions.init() NPEs in Extensions.scanForComponents.
    //
    // We scope the run to the two PR test classes (DoublePropertyTest,
    // PositiveDoublePropertyTest). test.patch adds `Extensions.init()` to
    // PositiveDoublePropertyTest's @BeforeClass, and Gradle deterministically runs
    // PositiveDoublePropertyTest BEFORE DoublePropertyTest. With one shared JVM
    // (forkEvery=0), PositiveDouble's init primes the static state so DoubleProperty
    // also passes in the fix stage. In the base run (no test.patch) PositiveDouble
    // is absent and DoubleProperty is un-primed -> it NPEs -> DoublePropertyTest is
    // F2P (fail base -> pass fix) and PositiveDoublePropertyTest is N2P (new).
    forkEvery = 0
    ignoreFailures = true
    maxParallelForks = 1
}
"""

FILTER_SCRIPT = """\
import sys
import re

patch_file = sys.argv[1]
output_file = sys.argv[2]

with open(patch_file, 'r', errors='replace') as f:
    content = f.read()

starts = [m.start() for m in re.finditer(r'^diff --git ', content, re.MULTILINE)]
parts = []
for i, s in enumerate(starts):
    end = starts[i + 1] if i + 1 < len(starts) else len(content)
    parts.append(content[s:end])

filtered = []
for part in parts:
    if not part.strip():
        continue
    if 'GIT binary patch' in part or 'Binary files' in part:
        continue
    first_line = part.split('\\n')[0]
    if re.search(
        r'\\.(png|jpg|jpeg|gif|ico|bin|woff|woff2|eot|ttf|otf|zip|gz|tar|svg|jar|class|war|ear)$',
        first_line, re.IGNORECASE):
        continue
    filtered.append(part)

with open(output_file, 'w') as f:
    f.write(''.join(filtered))
"""


class RobotBuilderImageBase(Image):
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
        # Gradle 5.4.1 supports Java 8-12; openjdk-11 is the safe pick.
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        dockerfile_content = """\
FROM {image_name}

{global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git curl python3 ca-certificates openjdk-11-jdk \\
    xvfb libxext6 libxrender1 libxtst6 libxi6 fontconfig \\
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/java-11-openjdk-$TARGETARCH
ENV PATH=$JAVA_HOME/bin:$PATH

{code}

WORKDIR /home/{repo_dir}

{clear_env}

"""
        return dockerfile_content.format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            repo_dir=REPO_DIR,
            clear_env=self.clear_env,
        )


class RobotBuilderImageDefault(Image):
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
        return RobotBuilderImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_binary_patch.py", FILTER_SCRIPT),
            File(".", "build_minimal.gradle", MINIMAL_BUILD_GRADLE),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Replace build.gradle with a minimal buildable equivalent: the original applies
# publishing/versioning plugins whose jcenter-only transitive dep
# grgit-core:3.0.0 is unfetchable today. The minimal file keeps the same
# compile+test classpath (all on Maven Central) plus test logging / headless.
cp /home/build_minimal.gradle build.gradle

# Pre-create the FRC_HOME extensions dir the tests scan (else
# Extensions.scanForComponents NPEs on a missing folder). Matches the FRC_HOME
# the test task exports in build.gradle.
mkdir -p /root/frchome/Robotbuilder/extensions

# Warm-build: downloads gradle 5.4.1 + deps and compiles main + test sources so
# the graded runs are fast. `|| true`: network fetches can be flaky here.
./gradlew classes testClasses --no-daemon --console=plain || true
""".format(repo_dir=REPO_DIR, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}
xvfb-run -a ./gradlew test --tests 'robotbuilder.data.properties.DoublePropertyTest' --tests 'robotbuilder.data.properties.PositiveDoublePropertyTest' --no-daemon --console=plain --continue
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

xvfb-run -a ./gradlew test --tests 'robotbuilder.data.properties.DoublePropertyTest' --tests 'robotbuilder.data.properties.PositiveDoublePropertyTest' --no-daemon --console=plain --continue
""".format(repo_dir=REPO_DIR),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo_dir}

python3 /home/filter_binary_patch.py /home/test.patch /tmp/test_filtered.patch
python3 /home/filter_binary_patch.py /home/fix.patch /tmp/fix_filtered.patch

if ! git apply --whitespace=nowarn /tmp/test_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/test_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/test_filtered.patch 2>/dev/null || true
    fi
fi

if ! git apply --whitespace=nowarn /tmp/fix_filtered.patch 2>/dev/null; then
    if ! git apply --whitespace=nowarn --3way /tmp/fix_filtered.patch 2>/dev/null; then
        git apply --whitespace=nowarn --reject /tmp/fix_filtered.patch 2>/dev/null || true
    fi
fi

xvfb-run -a ./gradlew test --tests 'robotbuilder.data.properties.DoublePropertyTest' --tests 'robotbuilder.data.properties.PositiveDoublePropertyTest' --no-daemon --console=plain --continue
""".format(repo_dir=REPO_DIR),
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
                        fi
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f "$HOME/.gradle/gradle.properties"
                """
                )

        dockerfile_content = """\
FROM {name}:{tag}

{global_env}
{proxy_setup}
{copy_commands}
{prepare_commands}
{proxy_cleanup}
{clear_env}

"""
        return dockerfile_content.format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            proxy_setup=proxy_setup,
            copy_commands=copy_commands,
            prepare_commands=prepare_commands,
            proxy_cleanup=proxy_cleanup,
            clear_env=self.clear_env,
        )


@Instance.register("wpilibsuite", "RobotBuilder")
class RobotBuilder(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RobotBuilderImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        passed_res = [
            re.compile(r"^(.+ > .+) PASSED$"),
            re.compile(r"^\s+Test (.+?) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^(.+ > .+) FAILED$"),
            re.compile(r"^\s+Test (.+?) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^(.+ > .+) SKIPPED$"),
            re.compile(r"^\s+Test (.+?) SKIPPED$"),
        ]

        for line in clean_log.splitlines():
            line = line.rstrip()
            for passed_re in passed_res:
                m = passed_re.match(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    passed_tests.discard(m.group(1))

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
