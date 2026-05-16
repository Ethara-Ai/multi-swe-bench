import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class JeecgBoot9357To5761ImageBase(Image):
    """Base image: Maven 3.9 + JDK 17 + git."""

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
        return "maven:3.9-eclipse-temurin-17"

    def image_tag(self) -> str:
        return "base-sb3"

    def workdir(self) -> str:
        return "base-sb3"

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

RUN sed -i 's|archive.ubuntu.com|ap-south-1.ec2.archive.ubuntu.com|g; s|security.ubuntu.com|ap-south-1.ec2.archive.ubuntu.com|g' /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || sed -i 's|archive.ubuntu.com|ap-south-1.ec2.archive.ubuntu.com|g; s|security.ubuntu.com|ap-south-1.ec2.archive.ubuntu.com|g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update && apt-get install -y git python3
RUN mkdir -p /root/.m2 && echo '<settings><mirrors><mirror><id>aliyun-https</id><mirrorOf>central</mirrorOf><url>https://maven.aliyun.com/repository/public</url></mirror></mirrors></settings>' > /root/.m2/settings.xml

{code}

{copy_commands}

{self.clear_env}

"""


class JeecgBoot9357To5761ImageDefault(Image):
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
        return JeecgBoot9357To5761ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _filter_binary_patches_script() -> str:
        return textwrap.dedent("""\
            python3 -c "
import re, sys
with open(sys.argv[1]) as f: content = f.read()
content = re.sub(r'diff --git [^\\n]*\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\n]*\\n)*Binary files [^\\n]*differ\\n?', '', content)
with open(sys.argv[2], 'w') as f: f.write(content)
" """)

    def _common_test_fixes(self) -> str:
        return textwrap.dedent("""\
            # === Fix pom.xml for JDK 17 compatibility ===
            python3 -c "
import re, glob
for p in glob.glob('/home/JeecgBoot/**/pom.xml', recursive=True):
    c = open(p).read()
    old = c
    
    # Convert HTTP repository URLs to HTTPS (Maven 3.9 blocks HTTP repos)
    c = re.sub(r'http://maven\\.(aliyun|jeecg)\\.org/', r'https://maven.\\1.org/', c)
    
    # Remove <skipTests> from surefire config (blocks -DskipTests=false)
    c = re.sub(r'(<artifactId>maven-surefire-plugin</artifactId>\\s*<configuration>)\\s*<skipTests>[^<]*</skipTests>\\s*', r'\\1', c)
    
    # Upgrade surefire to 3.2.5 (old 2.x can't discover JUnit 5 tests)
    if not re.search(r'<artifactId>maven-surefire-plugin</artifactId>\\s*<version>', c):
        c = re.sub(r'(<artifactId>maven-surefire-plugin</artifactId>)', r'\\1\\n<version>3.2.5</version>', c)
    else:
        c = re.sub(r'(<artifactId>maven-surefire-plugin</artifactId>\\s*<version>)[^<]*(</version>)', r'\\g<1>3.2.5\\2', c)
    
    if c != old:
        open(p, 'w').write(c)
"
            """)

    def files(self) -> list[File]:
        filter_script = self._filter_binary_patches_script()
        common_fixes = self._common_test_fixes()

        return [
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
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# Fetch the specific SHA first (may not be on default branch's history)
git fetch origin {pr.base.sha} 2>/dev/null || true
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export MAVEN_OPTS="--add-opens jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.jvm=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"

{common_fixes}

# Pre-cache Maven dependencies in image layer (avoids runtime network issues)
if [ -f jeecg-boot/pom.xml ]; then
    cd jeecg-boot
fi
mvn install -DskipTests -Dmaven.javadoc.skip=true -B -q || true
cd /home/{pr.repo}

""".format(pr=self.pr, common_fixes=common_fixes),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export CI=true
# --add-opens needed for old Lombok (pre-1.18) on JDK 17
export MAVEN_OPTS="--add-opens jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.jvm=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"

{common_fixes}

# Auto-detect pom location (jeecg-boot/ wrapper or root level)
[ -f jeecg-boot/pom.xml ] && cd jeecg-boot

mvn test -DskipTests=false -B -Dsurefire.useFile=false -Dmaven.test.failure.ignore=true 2>&1

""".format(pr=self.pr, common_fixes=common_fixes),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export CI=true
export MAVEN_OPTS="--add-opens jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.jvm=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"

{filter_script} /home/test.patch /tmp/test.patch.clean 2>/dev/null || cp /home/test.patch /tmp/test.patch.clean
git apply --whitespace=nowarn --reject /tmp/test.patch.clean 2>/dev/null || true

{common_fixes}

[ -f jeecg-boot/pom.xml ] && cd jeecg-boot

mvn test -DskipTests=false -B -Dsurefire.useFile=false -Dmaven.test.failure.ignore=true 2>&1

""".format(pr=self.pr, filter_script=filter_script, common_fixes=common_fixes),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

export CI=true
export MAVEN_OPTS="--add-opens jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.jvm=ALL-UNNAMED --add-opens jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.model=ALL-UNNAMED --add-opens java.base/java.lang=ALL-UNNAMED"

for PFILE in /home/test.patch /home/fix.patch; do
    CLEAN="/tmp/$(basename $PFILE).clean"
    {filter_script} "$PFILE" "$CLEAN" 2>/dev/null || cp "$PFILE" "$CLEAN"
done
git apply --whitespace=nowarn --reject /tmp/test.patch.clean /tmp/fix.patch.clean 2>/dev/null || true

{common_fixes}

[ -f jeecg-boot/pom.xml ] && cd jeecg-boot

mvn test -DskipTests=false -B -Dsurefire.useFile=false -Dmaven.test.failure.ignore=true 2>&1

""".format(pr=self.pr, filter_script=filter_script, common_fixes=common_fixes),
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


@Instance.register("jeecgboot", "JeecgBoot_9357_to_5761")
class JeecgBoot9357To5761(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JeecgBoot9357To5761ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes FIRST to prevent regex failures on colored output
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Maven surefire output:
        # "Tests run: 5, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 3.048 s -- in org.example.MyTest"
        re_surefire_summary = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*?-+\s*in\s+(\S+)"
        )

        # Individual test failure from surefire verbose:
        # "  testMethod(org.example.MyTest)  Time elapsed: 0.123 s  <<< FAILURE!"
        re_test_failure = re.compile(
            r"\s+(\S+)\(([^)]+)\)\s+Time elapsed:.*<<<\s*(FAILURE|ERROR)!"
        )

        # [ERROR] Tests run: ... format (error-level summary)
        re_error_summary = re.compile(
            r"\[ERROR\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+).*?-+\s*in\s+(\S+)"
        )

        for line in clean_log.splitlines():
            # Surefire summary line - class level
            m = re_surefire_summary.search(line)
            if m:
                runs = int(m.group(1))
                failures = int(m.group(2))
                errors = int(m.group(3))
                skips = int(m.group(4))
                class_name = m.group(5)
                if failures > 0 or errors > 0:
                    failed_tests.add(class_name)
                    passed_tests.discard(class_name)
                elif skips == runs and runs > 0:
                    skipped_tests.add(class_name)
                elif runs > 0:
                    if class_name not in failed_tests:
                        passed_tests.add(class_name)
                continue

            # [ERROR] summary (same format, just prefixed)
            m = re_error_summary.search(line)
            if m:
                failures = int(m.group(2))
                errors = int(m.group(3))
                class_name = m.group(5)
                if failures > 0 or errors > 0:
                    failed_tests.add(class_name)
                    passed_tests.discard(class_name)
                continue

            # Individual test method failure
            m = re_test_failure.search(line)
            if m:
                test_name = f"{m.group(2)}.{m.group(1)}"
                failed_tests.add(test_name)
                passed_tests.discard(test_name)
                continue

        # Clean up overlaps
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
