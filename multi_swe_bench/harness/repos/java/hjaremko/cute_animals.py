import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class CuteAnimalsImageBase(Image):
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
        # cute-animals is a single-module Spring Boot 2.2.7 / Gradle 6.3 build
        # that pins `sourceCompatibility = JavaVersion.VERSION_11` in
        # build.gradle, and .github/workflows/gradle.yml sets `java-version: 11`.
        # Gradle 6.3 predates JDK 17, so a newer JDK cannot run this build at
        # all — eclipse-temurin:11-jdk is the only toolchain that works.
        # The image is Ubuntu-based, so no archive.debian.org rewrite is needed.
        return "eclipse-temurin:11-jdk"

    def image_tag(self) -> str:
        # The tag names the PR this base was built for. The image is PR-specific
        # -- DockerfileEnhancer bakes `git checkout ${BASE_COMMIT}` into it -- so
        # a PR-agnostic "base" tag would let a second PR of this repo either
        # inherit this PR's pinned tree or silently overwrite the tag.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Maven Central rate-limits repeated CI-style builds from one address and
        # starts answering 429, which fails dependency resolution and leaves a
        # stage with zero tests -- observed on this repo. This Gradle init script
        # redirects Central to its Google-hosted mirror, which serves the same
        # artifacts and is not throttled. It ships as a readable file rather than
        # an inlined blob so the Dockerfile stays reviewable. It is an init
        # script, so build.gradle is never edited and the dataset's own patches
        # still apply cleanly.
        return [
            File(
                ".",
                "init.gradle",
                """def MIRROR = 'https://maven-central.storage-download.googleapis.com/maven2/'
def isThrottled = { u -> u != null && (u.contains('repo.maven.apache.org') || u.contains('repo1.maven.org')) }
def rewrite = { repos ->
    repos.all { r ->
        try { if (r.hasProperty('url') && isThrottled(r.url?.toString())) r.url = uri(MIRROR) } catch (ignored) {}
    }
}
gradle.settingsEvaluated { s ->
    try { s.pluginManagement.repositories { gradlePluginPortal(); maven { url MIRROR } } } catch (ignored) {}
    try { rewrite(s.pluginManagement.repositories) } catch (ignored) {}
    try { rewrite(s.dependencyResolutionManagement.repositories) } catch (ignored) {}
    try { s.dependencyResolutionManagement.repositories { maven { url MIRROR } } } catch (ignored) {}
}
gradle.allprojects { p ->
    rewrite(p.repositories)
    try { rewrite(p.buildscript.repositories) } catch (ignored) {}
}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # dependency() returns a string, so DockerfileEnhancer processes this
        # file: it prepends the infrastructure block (build args, proxy env, CA
        # certs) and rewrites `code` into
        #     clone "${REPO_URL}" + checkout ${BASE_COMMIT} + the hardening block
        # so the checked-out tree and the git-history isolation both land here,
        # in the base image.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl unzip \\
    && rm -rf /var/lib/apt/lists/*

# gradle/wrapper/gradle-wrapper.properties pins Gradle 6.3, but the wrapper's
# own downloader follows services.gradle.org's 307 to GitHub releases with a
# plain HttpURLConnection and fails intermittently with "Connection refused" —
# observed twice, and when it happens during prepare.sh the image ships with no
# Gradle at all and every stage collects zero tests. Install the same pinned
# 6.3 with curl, which retries and handles the redirect, and call `gradle`
# instead of `./gradlew` in the scripts.
RUN curl -fsSL --retry 5 --retry-delay 3 -o /tmp/gradle.zip \\
    https://services.gradle.org/distributions/gradle-6.3-bin.zip \\
    && echo "038794feef1f4745c6347107b6726279d1c824f3fc634b60f86ace1e9fbd1768  /tmp/gradle.zip" | sha256sum -c - \\
    && unzip -q /tmp/gradle.zip -d /opt \\
    && ln -sf /opt/gradle-6.3/bin/gradle /usr/local/bin/gradle \\
    && rm -f /tmp/gradle.zip

COPY init.gradle /root/.gradle/init.gradle

{code}

{self.clear_env}

"""


class CuteAnimalsImageDefault(Image):
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
        return CuteAnimalsImageBase(self.pr, self._config)

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
                "print_test_results.sh",
                """#!/bin/bash
# Concatenate every JUnit XML report so parse_log can extract test outcomes.
# Gradle's `test` task writes one report per test class to
# build/test-results/test/TEST-<fqcn>.xml. The XML is used instead of Gradle's
# console output because the default console prints nothing per test, and
# wiring `testLogging` in would mean editing build.gradle, which the dataset's
# own patches are applied on top of.
REPO_DIR="/home/{repo}"
echo "===== BEGIN TEST RESULTS ====="
find "$REPO_DIR" -path '*/build/test-results/test/TEST-*.xml' -exec cat {{}} \\; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# Set in the scripts rather than as ENV in the base image, so the base
# Dockerfile carries exactly the one ENV block the pipeline injects.
# The heap goes through -Dorg.gradle.jvmargs, not a bare -Xmx: a bare -Xmx
# sizes the `gradle` launcher, which is not the process that runs the build.
# Gradle runs the build in a forked JVM whose heap comes from jvmargs, so a
# bare -Xmx3g left that JVM on Gradle's 512m default.
export CI=true
export GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Warm the plugin graph and the FULL test runtime classpath at BUILD time so the
# three stages never need the network. This must be `test`, not `testClasses`:
# testClasses resolves only the compile classpath, which silently omits
# `runtimeOnly 'org.postgresql:postgresql'` (build.gradle) -- that jar is needed
# to launch the test JVM, so every stage would reach Maven Central for it and
# die on a 429/outage with zero tests collected. Observed exactly that.
# The cap is generous because a multi-arch build runs this step under QEMU
# emulation for the non-native platform, where it is an order of magnitude
# slower; on the native platform it finishes in about a minute.
timeout --kill-after=30 3600 gradle test --no-daemon --continue || true
# Running `test` above writes JUnit XML into the image. Delete it: otherwise
# print_test_results.sh would replay these stale reports in a stage whose own
# run produced nothing, and Gradle would report the test task UP-TO-DATE and
# skip re-running it in each stage.
rm -rf build/test-results build/reports/tests

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

cd /home/{repo}
rc=0
timeout --kill-after=30 1200 gradle test --no-daemon --continue || rc=$?
bash /home/print_test_results.sh
# Gradle exits 1 when tests merely fail, which is the expected outcome of the
# baseline and test stages -- that must not abort the script before the results
# are printed. Any other status means the runner never got as far as running
# tests (dependency resolution failure, OOM, timeout kill), and the empty result
# block above would otherwise be indistinguishable from a clean run. Surface it
# instead of swallowing it with `|| true`.
if [ "$rc" -gt 1 ]; then
    echo "TEST RUNNER DID NOT COMPLETE (exit $rc): results above are not trustworthy"
    exit "$rc"
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
rc=0
timeout --kill-after=30 1200 gradle test --no-daemon --continue || rc=$?
bash /home/print_test_results.sh
# Gradle exits 1 when tests merely fail, which is the expected outcome of the
# baseline and test stages -- that must not abort the script before the results
# are printed. Any other status means the runner never got as far as running
# tests (dependency resolution failure, OOM, timeout kill), and the empty result
# block above would otherwise be indistinguishable from a clean run. Surface it
# instead of swallowing it with `|| true`.
if [ "$rc" -gt 1 ]; then
    echo "TEST RUNNER DID NOT COMPLETE (exit $rc): results above are not trustworthy"
    exit "$rc"
fi

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export GRADLE_OPTS="-Dorg.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rc=0
timeout --kill-after=30 1200 gradle test --no-daemon --continue || rc=$?
bash /home/print_test_results.sh
# Gradle exits 1 when tests merely fail, which is the expected outcome of the
# baseline and test stages -- that must not abort the script before the results
# are printed. Any other status means the runner never got as far as running
# tests (dependency resolution failure, OOM, timeout kill), and the empty result
# block above would otherwise be indistinguishable from a clean run. Surface it
# instead of swallowing it with `|| true`.
if [ "$rc" -gt 1 ]; then
    echo "TEST RUNNER DID NOT COMPLETE (exit $rc): results above are not trustworthy"
    exit "$rc"
fi

""".format(repo=self.pr.repo),
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


@Instance.register("hjaremko", "cute-animals")
class CuteAnimals(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CuteAnimalsImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # JUnit XML emitted by Gradle's `test` task under
        # build/test-results/test/TEST-*.xml and concatenated by
        # print_test_results.sh:
        #   <testcase name="X()" classname="pkg.Y" time=".."/>   -> pass
        #   <testcase ...><failure .../></testcase>              -> fail
        #   <testcase ...><error .../></testcase>                -> fail
        #   <testcase ...><skipped/></testcase>                  -> skip
        # Attributes are pulled out order-independently rather than matched in a
        # fixed order, because the order is a property of the producer and not
        # of the format.
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        # The id is "<repo-relative source file>::<method>", the path-embedded
        # shape pytest emits and the one report.py's matchers are built around:
        # _test_name_matches_files splits on the first "::" and compares the head
        # to a patch-touched path, so
        # "src/test/java/pl/uj/io/cuteanimals/action/ability/FocusTest.java::shouldDrain20Mana"
        # resolves back to exactly the file the test patch adds. The XML only
        # carries a fully-qualified class name, so the file is reconstructed from
        # it: this is a single-module Gradle build, so every test class lives
        # under the standard src/test/java source set, and a "$" marks a nested
        # class, which is declared in its outer class's file.
        for m in testcase_re.finditer(test_log):
            attrs = m.group(1)
            nm = name_re.search(attrs)
            cn = classname_re.search(attrs)
            if not nm or not cn:
                continue
            source_file = (
                "src/test/java/"
                + cn.group(1).split("$", 1)[0].replace(".", "/")
                + ".java"
            )
            # JUnit renders a no-arg method as "name()"; drop the empty parens so
            # the id reads like the rest of the corpus. A parameterised name
            # carries its arguments and is left intact.
            method = nm.group(1)
            method = method[:-2] if method.endswith("()") else method
            test_id = f"{source_file}::{method}"
            closing = m.group(2)
            inner = m.group(3) or ""

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # A test id can appear more than once across retries or reports; resolve
        # precedence deterministically so the buckets never overlap. Failure wins:
        # crediting a test that was ever observed failing as passed is the
        # unsafe direction, and this repo has a nondeterministic test.
        passed_tests -= failed_tests
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
