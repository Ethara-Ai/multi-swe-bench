import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Both patches land entirely in the webapp module (src/main/java and src/test/java),
# so that is the graded module. `-am` also builds the commons/* modules it depends on;
# without it the reactor cannot resolve io.github.microcks:microcks-util and nothing
# compiles. Building the whole reactor instead would add minions/async and distro,
# which are unrelated here and pull in far heavier dependencies.
MODULE = "webapp"

# -B                                 batch mode, no interactive/ANSI progress
# -Dmaven.test.failure.ignore=true   do NOT abort the build on a failing test. Without
#                                    it surefire fails the module on the first failure
#                                    and the remaining tests never run, so the stage
#                                    reports a truncated suite and fabricates
#                                    transitions. This is the Maven equivalent of
#                                    --no-fail-fast and is applied to all three stages.
# -Dspotless.check.skip=true         the root pom binds spotless; a formatting
#                                    violation would fail the build before any test runs
# -Djacoco.skip=true                 coverage agent adds time and nothing here reads it
MVN_FLAGS = (
    "-B -Dmaven.test.failure.ignore=true "
    "-Dspotless.check.skip=true -Djacoco.skip=true"
)


class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        # webapp/pom.xml declares <java.version>21</java.version> and the root pom uses
        # the Spring Boot parent, so the toolchain must be JDK 21. The repo ships no
        # Maven wrapper (no mvnw at the base commit), so Maven has to come from the
        # image rather than the project.
        #
        # Single layer, deliberately: docker_util._get_container_builder() routes any
        # build with a platform set through the docker-container buildx driver, which
        # cannot see images loaded into the local daemon, so a `FROM <our-own-base>`
        # split is unbuildable here. Returning a str also keeps DockerfileEnhancer
        # engaged, which performs the BASE_COMMIT checkout and history scrub.
        return "maven:3.9-eclipse-temurin-21"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Surefire writes one JUnit XML per test class under target/surefire-reports/.
        # Those are printed for parse_log rather than scraping Maven's console output,
        # where a test name is indented under its class and only the class appears at
        # the margin.
        #
        # The `find` covers every module the reactor built, not just webapp, so a test
        # that moves module still gets reported instead of silently vanishing.
        #
        # `|| true` so a non-zero exit (expected in the test stage) does not kill the
        # script before the XML is printed. A genuinely broken build cannot hide behind
        # it, because the image refuses to seal unless test-compile succeeded at
        # BASE_COMMIT.
        cmd = (
            f"cd /home/{self.pr.repo}\n"
            f"mvn {MVN_FLAGS} -pl {MODULE} -am test || true\n"
            "echo '--- SUREFIRE XML ---'\n"
            "find . -path '*/target/surefire-reports/*.xml' -exec cat {} + 2>/dev/null "
            "|| echo 'no surefire xml produced'"
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
{cmd}
""".format(cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image}

{self.global_env}

# Maven and its plugins draw progress and status with non-ASCII characters. The harness
# decodes buildx output with the platform default codec (cp1252 on Windows), where those
# bytes are undefined and abort the build with "'charmap' codec can't decode byte ...".
# -B alone is not enough: downloads and some plugins still emit them, so the encoding is
# forced too.
ENV MAVEN_OPTS="-Dfile.encoding=UTF-8 -Djansi.force=false -Djansi.passthrough=false"
ENV MAVEN_ARGS="-B -ntp"
ENV TERM=dumb

WORKDIR /home/

{code}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset --hard
# and checkout BASE_COMMIT, then the history-scrub block whose assertions fail the build
# unless HEAD is exactly BASE_COMMIT. Repeating any of that here would be dead code. The
# WORKDIR is kept so the Maven steps below do not depend on the enhancer's line ordering.
WORKDIR /home/{self.pr.repo}

# Warm the local Maven repository at image-build time so the three graded stages do not
# each resolve the Spring Boot dependency tree over the network. A stage that has to
# reach the network mid-run can fail for reasons unrelated to the patch under test.
# Failure is tolerated here: the stages re-resolve whatever is missing.
RUN mvn {MVN_FLAGS} -pl {MODULE} -am dependency:go-offline > /dev/null 2>&1 || true

# Refuse to seal an image whose graded stages could not report anything. If the reactor
# cannot compile the test sources at BASE_COMMIT - a missing module, a JDK mismatch, an
# unresolvable dependency - every stage would report zero tests, which reads downstream
# as "these tests do not exist" rather than as a broken image, and the harness scores
# that as a valid n2p-only resolve.
RUN mvn {MVN_FLAGS} -pl {MODULE} -am test-compile

WORKDIR /home/

{copy_commands}
{self.clear_env}

"""


@Instance.register("microcks", "microcks")
class Microcks(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # Attribute ORDER must not matter. Surefire emits name before classname; other
        # runners emit classname first. A regex hardcoding one order silently drops every
        # testcase from the other, which reads downstream as "those tests do not exist"
        # rather than as a parse failure.
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        attr_re = re.compile(r'\b(name|classname)="([^"]*)"')

        def unescape(s: str) -> str:
            # &amp; LAST, or "&amp;lt;" is unescaped twice into "<".
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&apos;", "'"), ("&amp;", "&")):
                s = s.replace(a, b)
            return s

        for m in testcase_re.finditer(log):
            attrs = dict(attr_re.findall(m.group(1)))
            name = unescape(attrs.get("name", ""))
            classname = unescape(attrs.get("classname", ""))
            if not name and not classname:
                continue
            closing, inner = m.group(2), m.group(3) or ""

            # classname is the fully qualified test class
            # (io.github.microcks.util.grpc.GrpcMetadataUtilTest), so this is unique
            # even when two classes share a method name.
            test_id = f"{classname}.{name}" if classname else name

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Surefire can rerun a flaky test and emit it twice; enforce one bucket each, or
        # the stage comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
