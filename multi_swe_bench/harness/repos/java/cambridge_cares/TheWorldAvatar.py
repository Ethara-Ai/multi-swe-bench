import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class TheWorldAvatarImageBase(Image):
    """Repo-level base: JDK 11 + Maven. TheWorldAvatar is a monorepo; the graded module is
    Agents/DistrictHeatingAgent/DistrictHeatingAgent, a Maven WAR project that PR 339 creates
    from scratch. Its pom pins <release>11</release> and the module's own Dockerfile builds on
    maven:3.6-openjdk-11-slim, so JDK 11 is the toolchain. eclipse-temurin is used instead of
    the buster-based openjdk-11 tag because the latter is EOL Debian (R11) and publishes no
    arm64 manifest."""

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
        return "maven:3.8-eclipse-temurin-11"

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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

{self.clear_env}

"""


class TheWorldAvatarImageDefault(Image):
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
        return TheWorldAvatarImageBase(self.pr, self._config)

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
                "EnableSurefire.java",
                """package twa;

import java.util.List;

import org.apache.maven.AbstractMavenLifecycleParticipant;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/**
 * DistrictHeatingAgent/pom.xml configures maven-surefire-plugin with a literal
 * <skipTests>true</skipTests>. A literal in the POM outranks the -DskipTests user
 * property, so the gold tests never execute and every stage collects zero results.
 * Verified in a container: `mvn test` and `mvn surefire:test -DskipTests=false` both
 * print "Tests are skipped."; with this extension the same POM reports "Tests run: 1".
 *
 * Dropping the element from the effective model at afterProjectsRead restores
 * surefire's own default (false). The POM on disk is never rewritten, so the gold
 * patches still apply cleanly (R22).
 */
public class EnableSurefire extends AbstractMavenLifecycleParticipant {

    @Override
    public void afterProjectsRead(MavenSession session) {
        for (MavenProject project : session.getProjects()) {
            if (project.getBuild() == null) {
                continue;
            }
            List<Plugin> plugins = project.getBuild().getPlugins();
            for (Plugin plugin : plugins) {
                if (!"maven-surefire-plugin".equals(plugin.getArtifactId())) {
                    continue;
                }
                clear((Xpp3Dom) plugin.getConfiguration());
                for (PluginExecution execution : plugin.getExecutions()) {
                    clear((Xpp3Dom) execution.getConfiguration());
                }
            }
        }
    }

    private static void clear(Xpp3Dom dom) {
        if (dom == null) {
            return;
        }
        for (int i = dom.getChildCount() - 1; i >= 0; i--) {
            String name = dom.getChild(i).getName();
            if ("skipTests".equals(name) || "skip".equals(name) || "skipExec".equals(name)) {
                dom.removeChild(i);
            }
        }
    }
}

""",
            ),
            File(
                ".",
                "components.xml",
                """<component-set>
  <components>
    <component>
      <role>org.apache.maven.AbstractMavenLifecycleParticipant</role>
      <role-hint>twa-enable-surefire</role-hint>
      <implementation>twa.EnableSurefire</implementation>
      <isolated-realm>false</isolated-realm>
    </component>
  </components>
</component-set>

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

# jps-parent-pom and jps-base-lib are published only to
# https://maven.pkg.github.com/cambridge-cares/TheWorldAvatar/, which answers 401 to
# anonymous requests even though the repository is public, so the graded module cannot
# resolve them over the network. Both are sources in this monorepo, so build them here
# and install them under the coordinates DistrictHeatingAgent/pom.xml asks for. Only the
# local Maven repository is written; no tracked file is edited (R22).
#
# Cost of the substitution, stated plainly: at this base commit the in-tree sources carry
# jps-parent-pom 2.2.0 and jps-base-lib 1.33.0, while the agent asks for 2.0.0 and 1.31.1.
# The published 1.31.1 artifact is unreachable, so the in-tree build stands in for it.
cd /home/{pr.repo}/Agents/utils/parent-pom
mvn -B -q install:install-file -Dfile=pom.xml -DpomFile=pom.xml \\
    -DgroupId=uk.ac.cam.cares.jps -DartifactId=jps-parent-pom -Dversion=1.0.0 -Dpackaging=pom || true
mvn -B -q install:install-file -Dfile=pom.xml -DpomFile=pom.xml \\
    -DgroupId=uk.ac.cam.cares.jps -DartifactId=jps-parent-pom -Dversion=2.0.0 -Dpackaging=pom || true

# -Dmdep.skip=true switches off the parent POM's maven-dependency-plugin executions, which
# unpack a java-logging-dev zip from that same 401 registry.
cd /home/{pr.repo}/JPS_BASE_LIB
mvn -B -DskipTests -Dmdep.skip=true -Dmaven.javadoc.skip=true package || true
mvn -B -q install:install-file -Dfile=target/jps-base-lib.jar -DpomFile=pom.xml \\
    -DgroupId=uk.ac.cam.cares.jps -DartifactId=jps-base-lib -Dversion=1.31.1 -Dpackaging=jar || true

# Compile the surefire-enabling Maven core extension described in EnableSurefire.java and
# stage it where -Dmaven.ext.class.path can pick it up.
mkdir -p /home/mvn-ext/META-INF/plexus
mv /home/components.xml /home/mvn-ext/META-INF/plexus/components.xml
javac -cp "$(ls $MAVEN_HOME/lib/*.jar | tr '\\n' ':')" -d /home/mvn-ext /home/EnableSurefire.java

# Warm the graded module's dependencies. Its pom.xml arrives with the fix patch, so it
# cannot be resolved here; fetch the coordinates that pom declares directly.
cd /home/{pr.repo}
for artifact in \\
    junit:junit:4.12 \\
    org.mockito:mockito-inline:3.6.28 \\
    net.bytebuddy:byte-buddy:1.11.20 \\
    javax.servlet:javax.servlet-api:3.1.0 \\
    commons-io:commons-io:2.6 \\
    xml-apis:xml-apis:1.4.01 \\
    com.jayway.jsonpath:json-path:2.4.0 \\
    org.locationtech.jts:jts-core:1.15.1 \\
    org.apache.maven.plugins:maven-surefire-plugin:2.12.4 \\
    org.apache.maven.plugins:maven-compiler-plugin:3.8.0 \\
    org.apache.maven.plugins:maven-resources-plugin:3.2.0 ; do
  mvn -B -q dependency:get -Dartifact=$artifact || true
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
# PR 339 creates the whole module, pom.xml included, so at the run and test stages there is
# nothing to build. Guard on the pom so those stages report zero tests instead of aborting on
# a missing -f target; the fix stage is where the gold tests first exist and run. A compiled
# language grading its new tests n2p rather than f2p is expected and Report.check() accepts it.
#
# -DenableAssertions=false: surefire runs the JVM with -ea by default. jps-base-lib pulls in
# com.ibm.icu, whose OlsonTimeZone.setID trips an internal `assert` while resolving the
# container's default zone, so HeatNetworkInputAgentLauncher's <clinit> dies before any test
# body runs. That took down all three tests in that class -- two of them as
# "class redefinition failed: invalid class", because Mockito.mockStatic instruments a class
# whose static initialiser had already failed. Assertions off is how the agent runs in its own
# Tomcat image, and it is not a per-zone quirk: TZ=Asia/Shanghai, Europe/London and
# America/New_York were each tried with -ea on and all three failed identically.
rm -rf Agents/DistrictHeatingAgent/DistrictHeatingAgent/target
if [ -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml ]; then
  mvn -B -Dmaven.ext.class.path=/home/mvn-ext \\
      -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml clean test \\
      -Dstyle.color=never -Dmaven.test.failure.ignore=true -DenableAssertions=false
fi
echo '===== BEGIN TEST RESULTS ====='
if [ -d Agents/DistrictHeatingAgent ]; then
  find Agents/DistrictHeatingAgent -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\;
fi
echo '===== END TEST RESULTS ====='

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
rm -rf Agents/DistrictHeatingAgent/DistrictHeatingAgent/target
if [ -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml ]; then
  mvn -B -Dmaven.ext.class.path=/home/mvn-ext \\
      -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml clean test \\
      -Dstyle.color=never -Dmaven.test.failure.ignore=true -DenableAssertions=false
fi
echo '===== BEGIN TEST RESULTS ====='
if [ -d Agents/DistrictHeatingAgent ]; then
  find Agents/DistrictHeatingAgent -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\;
fi
echo '===== END TEST RESULTS ====='

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
rm -rf Agents/DistrictHeatingAgent/DistrictHeatingAgent/target
if [ -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml ]; then
  mvn -B -Dmaven.ext.class.path=/home/mvn-ext \\
      -f Agents/DistrictHeatingAgent/DistrictHeatingAgent/pom.xml clean test \\
      -Dstyle.color=never -Dmaven.test.failure.ignore=true -DenableAssertions=false
fi
echo '===== BEGIN TEST RESULTS ====='
if [ -d Agents/DistrictHeatingAgent ]; then
  find Agents/DistrictHeatingAgent -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \\;
fi
echo '===== END TEST RESULTS ====='

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("cambridge-cares", "TheWorldAvatar")
class TheWorldAvatar(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TheWorldAvatarImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_testcase = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        re_name = re.compile(r'\bname="([^"]*)"')
        re_classname = re.compile(r'\bclassname="([^"]*)"')

        for testcase_match in re_testcase.finditer(clean_log):
            name_match = re_name.search(testcase_match.group(1))
            classname_match = re_classname.search(testcase_match.group(1))
            if not name_match or not classname_match:
                continue

            # `<fully.qualified.Class>#<method>` is the shape report.py's _file_hosts_test
            # resolves back to `<SimpleName>.java`: it splits on '#', then drops the package
            # prefix. A `Class.method` id would resolve to the method name instead and land
            # every credited test in fix_patch_authored_candidates (R20).
            test_name = f"{classname_match.group(1)}#{name_match.group(1)}"
            body = testcase_match.group(3) or ""

            if "<failure" in body or "<error" in body:
                failed_tests.add(test_name)
            elif "<skipped" in body:
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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
