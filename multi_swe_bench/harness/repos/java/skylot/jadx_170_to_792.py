import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.skylot._common import (
    _binary_extract_shell,
    _binary_restore_shell,
    _extract_end_tag,
    _filter_binary_patches,
)

# init.gradle: ensure mavenCentral() is available, substitute missing
# jcommander:1.74 with 1.72 (jcenter is dead; 1.74 was jcenter-only), and emit
# per-test PASSED/FAILED events. Lives at /root/.gradle (outside the git tree),
# so the hardening pass leaves it untouched.
_INIT_GRADLE = """\
allprojects {
    repositories {
        mavenCentral()
        maven { url "https://plugins.gradle.org/m2/" }
    }
    configurations.all {
        resolutionStrategy.dependencySubstitution {
            substitute module("com.beust:jcommander:1.74") with module("com.beust:jcommander:1.72")
        }
    }
    tasks.withType(Test) { test ->
        test.testLogging {
            events "passed", "failed", "skipped"
            exceptionFormat "short"
            showStandardStreams = false
        }
    }
}
"""


class JadxJdk8Era2ImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "eclipse-temurin:8"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Sets the JVM/Gradle env, installs the init.gradle to /root, and
        # stages the helper scripts + patches into /home/. prepare.sh then warms
        # the Gradle build and pre-extracts binary fixtures while git history
        # still exists (the hardening pass removes it afterward). git/curl/
        # ca-certificates are already in the default package set.
        return (
            "ENV LC_ALL=C.UTF-8\n"
            'ENV JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8"\n'
            'ENV GRADLE_OPTS="-Dorg.gradle.daemon=false -Dorg.gradle.jvmargs=-Xmx2g"\n'
            "COPY init.gradle /root/.gradle/init.gradle\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def files(self) -> list[File]:
        filtered_fix_patch = _filter_binary_patches(self.pr.fix_patch)
        filtered_test_patch = _filter_binary_patches(self.pr.test_patch)
        end_tag = _extract_end_tag(getattr(self.pr.base, "label", "") or "")
        bin_extract = _binary_extract_shell(
            self.pr.fix_patch, self.pr.test_patch, end_tag
        )
        bin_restore_fix = _binary_restore_shell(
            self.pr.fix_patch, self.pr.test_patch, end_tag
        )
        bin_restore_test = _binary_restore_shell("", self.pr.test_patch, end_tag)
        repo = self.pr.repo

        prepare_sh = (
            "#!/bin/bash\n"
            "# Repo already cloned + checked out at ${BASE_COMMIT} and hardened by\n"
            "# Image.dockerfile(); this warms the Gradle build and pre-extracts binary\n"
            "# fixtures before the hardening pass removes git history.\n"
            "set -e\n"
            "\n"
            f"mkdir -p /home/{repo}\n"
            f"cd /home/{repo}\n"
            "git reset --hard || true\n"
            "\n"
            + bin_extract
            + "\n"
            "./gradlew --no-daemon assemble || true\n"
        )
        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f"mkdir -p /home/{repo}\n"
            f"cd /home/{repo}\n"
            "./gradlew --no-daemon test --continue\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f"mkdir -p /home/{repo}\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            + bin_restore_test
            + "./gradlew --no-daemon test --continue\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            "\n"
            f"mkdir -p /home/{repo}\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            + bin_restore_fix
            + "./gradlew --no-daemon test --continue\n"
        )

        return [
            File(".", "init.gradle", _INIT_GRADLE),
            File(".", "fix.patch", f"{filtered_fix_patch}"),
            File(".", "test.patch", f"{filtered_test_patch}"),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]


@Instance.register("skylot", "jadx_170_to_792")
class Jadx170To792(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JadxJdk8Era2ImageDefault(self.pr, self._config)

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
            re.compile(r"^(.+ > .+) PASSED$"),
        ]

        failed_res = [
            re.compile(r"^> Task :(\S+) FAILED$"),
            re.compile(r"^(.+ > .+) FAILED$"),
        ]

        skipped_res = [
            re.compile(r"^> Task :(\S+) SKIPPED$"),
            re.compile(r"^(.+ > .+) SKIPPED$"),
        ]

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
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
