import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class Resilience4jImageBase(Image):
    """Base image with JDK 17 for Era 3 (PR >= 1713): Gradle 7.4+, Java 17."""

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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
WORKDIR /home/
RUN apt-get update && apt-get install -y git openjdk-17-jdk

{code}

{copy_commands}

{self.clear_env}

"""


class Resilience4jImageBaseJDK8(Image):
    """Base image with JDK 8 for Era 1+2 (PR <= 1482): Gradle 3.x-7.x, Java 8."""

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
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "base-jdk-8"

    def workdir(self) -> str:
        return "base-jdk-8"

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
RUN apt-get update && apt-get install -y git openjdk-8-jdk

{code}

{copy_commands}

{self.clear_env}

"""


class Resilience4jImageDefault(Image):
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
        # Era 3: PR >= 1713, JDK 17
        if self.pr.number >= 1713:
            return Resilience4jImageBase(self.pr, self._config)
        # Era 1+2: PR <= 1482, JDK 8
        return Resilience4jImageBaseJDK8(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _gradle_jvmargs(self) -> str:
        return (
            'sed -i "/^org.gradle.jvmargs=/d" gradle.properties 2>/dev/null || true\n'
            'echo "org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=512m" >> gradle.properties'
        )

    def _cleanup_era1(self) -> str:
        """Dead plugin cleanup for Era 1 (PR <= 373): Gradle 3.x, buildscript style."""
        return textwrap.dedent("""\
            # --- Era 1 cleanup: Gradle 3.x buildscript-style dead plugins ---
            # STEP 1: Remove multi-line blocks FIRST (before sed destroys block headers)
            for BLOCK in 'cobertura' 'jmh' 'coveralls' 'artifactory'; do
                awk -v block="$BLOCK" '
                    BEGIN { depth=0; skip=0 }
                    skip==0 && $0 ~ block" \\\\{" { skip=1; depth=1; next }
                    skip==0 && $0 ~ "^"block" \\\\{" { skip=1; depth=1; next }
                    skip==0 && $0 ~ "^    "block" \\\\{" { skip=1; depth=1; next }
                    skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                    { print }
                ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            done
            # Also remove tasks.coveralls block
            awk '
                BEGIN { depth=0; skip=0 }
                skip==0 && /tasks\\.coveralls/ && /\\{/ { skip=1; depth=1; next }
                skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                { print }
            ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            # Remove test { dependsOn(subprojects.test) } block
            awk '
                BEGIN { depth=0; skip=0 }
                skip==0 && /^test \\{/ { skip=1; depth=1; next }
                skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                { print }
            ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            # STEP 2: Now safe to use sed for line-level deletions
            # build.gradle: remove dead classpath dependencies (specific patterns)
            sed -i "/gradle-cobertura-plugin/d" build.gradle || true
            sed -i "/coveralls-gradle-plugin/d" build.gradle || true
            sed -i "/gradle-bintray-plugin/d" build.gradle || true
            sed -i "/build-info-extractor-gradle/d" build.gradle || true
            sed -i "/asciidoctor-gradle-plugin/d" build.gradle || true
            sed -i "/org.ajoberstar.*gradle-git/d" build.gradle || true
            sed -i "/jmh-gradle-plugin/d" build.gradle || true
            sed -i "/jcstress-gradle-plugin/d" build.gradle || true
            # build.gradle: remove apply plugin lines
            sed -i "/apply plugin: 'com.github.kt3k.coveralls'/d" build.gradle || true
            sed -i "/apply plugin: 'com.jfrog.bintray'/d" build.gradle || true
            sed -i "/apply plugin: 'net.saliman.cobertura'/d" build.gradle || true
            sed -i "/apply plugin: 'com.jfrog.artifactory'/d" build.gradle || true
            sed -i "/apply plugin: 'me.champeau.gradle.jmh'/d" build.gradle || true
            # build.gradle: remove artifactoryPublish reference
            sed -i "/artifactoryPublish/d" build.gradle || true
            # build.gradle: remove jmh dependency lines (both string and project refs)
            sed -i '/^[[:space:]]*jmh "/d' build.gradle || true
            sed -i '/^[[:space:]]*jmh project/d' build.gradle || true
            # build.gradle: remove def files = subprojects.collect... line
            sed -i "/files = subprojects.collect/d" build.gradle || true
            # build.gradle: replace jcenter with mavenCentral
            sed -i "s/jcenter()/mavenCentral()/g" build.gradle || true
            # publishing.gradle: remove bintray block
            if [ -f publishing.gradle ]; then
                awk '
                    BEGIN { depth=0; skip=0 }
                    skip==0 && /bintray \\{/ { skip=1; depth=1; next }
                    skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                    { print }
                ' publishing.gradle > publishing.gradle.tmp && mv publishing.gradle.tmp publishing.gradle || true
            fi
            # Subproject build files: remove jcstress plugin and jmh deps
            for f in resilience4j-bulkhead/build.gradle resilience4j-circuitbreaker/build.gradle resilience4j-circularbuffer/build.gradle; do
                [ -f "$f" ] && sed -i "/jcstress/d" "$f" || true
                [ -f "$f" ] && sed -i '/^[[:space:]]*jmh "/d' "$f" || true
                [ -f "$f" ] && sed -i '/^[[:space:]]*jmh project/d' "$f" || true
            done
            # resilience4j-all: remove jmh plugin and deps
            [ -f resilience4j-all/build.gradle ] && sed -i "/me.champeau.gradle.jmh/d" resilience4j-all/build.gradle || true
            [ -f resilience4j-all/build.gradle ] && sed -i '/^[[:space:]]*jmh "/d' resilience4j-all/build.gradle || true
            [ -f resilience4j-all/build.gradle ] && sed -i '/^[[:space:]]*jmh project/d' resilience4j-all/build.gradle || true
            # resilience4j-test: remove artifactoryPublish skip
            [ -f resilience4j-test/build.gradle ] && sed -i "/artifactoryPublish/d" resilience4j-test/build.gradle || true
            # settings.gradle: remove documentation project
            sed -i "/resilience4j-documentation/d" settings.gradle || true
            # settings.gradle: remove bom project (incompatible with Gradle 3.x)
            sed -i "/resilience4j-bom/d" settings.gradle || true""")

    def _cleanup_era2(self) -> str:
        """Dead plugin cleanup for Era 2 (PR 441-1482): Gradle 5.x+, plugins{} block style."""
        return textwrap.dedent("""\
            # --- Era 2 cleanup: Gradle 5.x+ plugins-block dead plugins ---
            # STEP 1: Remove multi-line blocks FIRST (before sed destroys block headers)
            for BLOCK in 'jmh' 'sonarqube' 'artifactory'; do
                awk -v block="$BLOCK" '
                    BEGIN { depth=0; skip=0 }
                    skip==0 && $0 ~ block" \\\\{" { skip=1; depth=1; next }
                    skip==0 && $0 ~ "^"block" \\\\{" { skip=1; depth=1; next }
                    skip==0 && $0 ~ "^    "block" \\\\{" { skip=1; depth=1; next }
                    skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                    { print }
                ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            done
            # Remove test { dependsOn(subprojects.test) } block
            awk '
                BEGIN { depth=0; skip=0 }
                skip==0 && /^test \\{/ { skip=1; depth=1; next }
                skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                { print }
            ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            # Remove afterEvaluate { jar { ... moduleName ... } } block if moduleName isn't defined
            # Wrap with hasProperty guard by replacing the afterEvaluate block
            awk '
                BEGIN { depth=0; skip=0 }
                skip==0 && /afterEvaluate/ && /\\{/ {
                    skip=1; depth=1
                    print "    if (project.hasProperty(\\\"moduleName\\\")) {"
                    print "    afterEvaluate {"
                    next
                }
                skip==1 {
                    for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }
                    if(depth<=0) { print $0; print "    }"; skip=0; next }
                }
                { print }
            ' build.gradle > build.gradle.tmp && mv build.gradle.tmp build.gradle || true
            # STEP 2: Now safe to use sed for line-level deletions
            # build.gradle: remove plugin IDs from plugins {} block
            sed -i "/com.jfrog.bintray/d" build.gradle || true
            sed -i "/com.jfrog.artifactory/d" build.gradle || true
            sed -i "/me.champeau.*jmh/d" build.gradle || true
            sed -i "/org.sonarqube/d" build.gradle || true
            # build.gradle: remove apply plugin lines
            sed -i "/apply plugin.*jmh/d" build.gradle || true
            sed -i "/apply plugin.*artifactory/d" build.gradle || true
            sed -i "/apply plugin.*bintray/d" build.gradle || true
            # build.gradle: remove artifactoryPublish references
            sed -i "/artifactoryPublish/d" build.gradle || true
            # build.gradle: replace jcenter with mavenCentral
            sed -i "s/jcenter()/mavenCentral()/g" build.gradle || true
            # build.gradle: remove tasks.check.dependsOn (all variants)
            sed -i "/tasks.check.dependsOn/d" build.gradle || true
            # build.gradle: remove tasks.jacocoRootTestReport.dependsOn tasks.test
            sed -i "/tasks.jacocoRootTestReport.dependsOn/d" build.gradle || true
            # build.gradle: remove jmh dependency lines (both string and project refs)
            sed -i '/^[[:space:]]*jmh "/d' build.gradle || true
            sed -i '/^[[:space:]]*jmh project/d' build.gradle || true
            # publishing.gradle: remove bintray block
            if [ -f publishing.gradle ]; then
                awk '
                    BEGIN { depth=0; skip=0 }
                    skip==0 && /bintray \\{/ { skip=1; depth=1; next }
                    skip==1 { for(i=1;i<=length($0);i++) { c=substr($0,i,1); if(c=="{") depth++; if(c=="}") depth-- }; if(depth<=0) { skip=0 }; next }
                    { print }
                ' publishing.gradle > publishing.gradle.tmp && mv publishing.gradle.tmp publishing.gradle || true
            fi
            # Subproject build files: remove jmh deps
            for f in resilience4j-bulkhead/build.gradle resilience4j-circuitbreaker/build.gradle; do
                [ -f "$f" ] && sed -i "/jmh project/d" "$f" || true
                [ -f "$f" ] && sed -i '/^[[:space:]]*jmh "/d' "$f" || true
            done
            # Clear test/documentation build files that only had dead plugin config
            for f in resilience4j-test/build.gradle resilience4j-documentation/build.gradle; do
                [ -f "$f" ] && echo "" > "$f" || true
            done
            # settings.gradle: remove documentation and bom projects
            sed -i "/resilience4j-documentation/d" settings.gradle || true
            sed -i "/resilience4j-bom/d" settings.gradle || true""")

    def _prepare_script(self) -> str:
        gradle_jvmargs = self._gradle_jvmargs()

        preamble = textwrap.dedent("""\
            #!/bin/bash
            set -e

            cd /home/{pr.repo}
            git reset --hard
            bash /home/check_git_changes.sh
            git checkout {pr.base.sha}
            bash /home/check_git_changes.sh
            {gradle_jvmargs}
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

                tasks.withType(Test) {{
                    testLogging {{
                        events 'passed', 'failed', 'skipped'
                        showStandardStreams = false
                        exceptionFormat 'full'
                    }}
                    outputs.upToDateWhen {{ false }}
                }}
            }}
            INITGRADLE""").format(
            pr=self.pr,
            gradle_jvmargs=gradle_jvmargs,
        )

        # Era-specific cleanup
        if self.pr.number <= 373:
            cleanup = self._cleanup_era1()
            # Gradle 3.x does NOT support --no-build-cache
            build_cmd = "./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 2 --continue || true"
        elif self.pr.number <= 1482:
            cleanup = self._cleanup_era2()
            build_cmd = "./gradlew clean test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue || true"
        else:
            # Era 3: clean build, no dead plugins
            cleanup = "# Era 3: no dead plugin cleanup needed"
            build_cmd = "./gradlew clean test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue || true"

        return f"{preamble}\n{cleanup}\n{build_cmd}\n"

    def _get_cleanup(self) -> str:
        """Return the era-specific cleanup script for the current PR."""
        if self.pr.number <= 373:
            return self._cleanup_era1()
        elif self.pr.number <= 1482:
            return self._cleanup_era2()
        else:
            return "# Era 3: no dead plugin cleanup needed"

    def _get_gradle_cmd(self) -> str:
        """Return the gradle test command for the current PR era."""
        if self.pr.number <= 373:
            return "./gradlew clean test --init-script ~/.gradle/init.gradle --max-workers 2 --continue"
        else:
            return "./gradlew clean test --no-build-cache --init-script ~/.gradle/init.gradle --max-workers 2 --continue"

    def _run_script(self) -> str:
        """Build the run.sh content.

        Resets to base commit, re-runs cleanup, then runs tests.
        No patches are applied — this tests the unmodified base state.
        """
        gradle_jvmargs = self._gradle_jvmargs()
        cleanup = self._get_cleanup()
        gradle_cmd = self._get_gradle_cmd()

        return f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git checkout -- .
{gradle_jvmargs}
{cleanup}
{gradle_cmd}
"""

    def _test_run_script(self) -> str:
        """Build the test-run.sh content.

        Resets to base commit, applies test patch, re-runs cleanup, then runs tests.
        """
        gradle_jvmargs = self._gradle_jvmargs()
        cleanup = self._get_cleanup()
        gradle_cmd = self._get_gradle_cmd()

        return f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git checkout -- .
python3 -c "
import re
with open('/home/test.patch') as f: content = f.read()
content = re.sub(r'diff --git [^\\n]*\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\n]*\\n)*Binary files [^\\n]*differ\\n?', '', content)
with open('/tmp/test.patch.clean', 'w') as f: f.write(content)
" 2>/dev/null || cp /home/test.patch /tmp/test.patch.clean
git apply --whitespace=nowarn /tmp/test.patch.clean
{gradle_jvmargs}
{cleanup}
{gradle_cmd}
"""

    def _fix_run_script(self) -> str:
        """Build the fix-run.sh content.

        Resets to base commit, applies test+fix patches, re-runs cleanup, then runs tests.
        """
        gradle_jvmargs = self._gradle_jvmargs()
        cleanup = self._get_cleanup()
        gradle_cmd = self._get_gradle_cmd()

        return f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git checkout -- .
for PFILE in /home/test.patch /home/fix.patch; do
    CLEAN="/tmp/$(basename $PFILE).clean"
    python3 -c "
import re
with open('$PFILE') as f: content = f.read()
content = re.sub(r'diff --git [^\\\\n]*\\\\n(?:(?:old|new|deleted|similarity|rename|index|copy)[^\\\\n]*\\\\n)*Binary files [^\\\\n]*differ\\\\n?', '', content)
with open('$CLEAN', 'w') as f: f.write(content)
" 2>/dev/null || cp "$PFILE" "$CLEAN"
done
git apply --whitespace=nowarn /tmp/test.patch.clean /tmp/fix.patch.clean
{gradle_jvmargs}
{cleanup}
{gradle_cmd}
"""

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
                self._prepare_script(),
            ),
            File(
                ".",
                "run.sh",
                self._run_script(),
            ),
            File(
                ".",
                "test-run.sh",
                self._test_run_script(),
            ),
            File(
                ".",
                "fix-run.sh",
                self._fix_run_script(),
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


@Instance.register("resilience4j", "resilience4j")
class Resilience4j(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Resilience4jImageDefault(self.pr, self._config)

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
            # New Gradle (5+): "> Task :taskName"
            re.compile(r"^> Task :(\S+)$"),
            re.compile(r"^> Task :(\S+) UP-TO-DATE$"),
            re.compile(r"^> Task :(\S+) FROM-CACHE$"),
            # Old Gradle (3.x): ":taskName"
            re.compile(r"^:(\S+)$"),
            re.compile(r"^:(\S+) UP-TO-DATE$"),
            re.compile(r"^:(\S+) FROM-CACHE$"),
            # Individual test method results (same format in all Gradle versions)
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

        # Clean up overlaps to avoid ValueError in TestResult.__post_init__
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
