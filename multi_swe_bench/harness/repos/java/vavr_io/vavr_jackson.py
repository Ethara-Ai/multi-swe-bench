import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# vavr-jackson's build.gradle carries no `testLogging` block, so the default
# Gradle console prints only task-level lines (`> Task :test`) and never the
# individual test methods. Without per-test identities parse_log can only see
# build tasks, and the whole f2p/p2p classification degenerates to "did the
# :test task pass". Injecting the events via an init script (rather than
# patching build.gradle) keeps the repo tree pristine so `git apply` of
# test.patch / fix.patch still applies cleanly.
#
# `displayGranularity 0` forces the full hierarchy onto every event line:
#   Gradle Test Run :test > Gradle Test Executor 3 \
#       > io.vavr.jackson.datatype.seq.ArrayTest > testSerialize() PASSED
_INIT_GRADLE = """\
allprojects {
    tasks.withType(Test).configureEach {
        testLogging {
            events "passed", "failed", "skipped"
            showStandardStreams = false
            displayGranularity = 0
            exceptionFormat = "short"
        }
        afterTest { desc, result ->
            logger.lifecycle("MSB_TEST|" + result.resultType + "|" + desc.className + "|" + desc.name)
        }
    }
}
"""

# Java source root for tests in this repo (standard Gradle/Maven layout, confirmed
# at base.sha: every test lives under src/test/java/<package>/<Class>.java).
_TEST_SOURCE_ROOT = "src/test/java"

# Same invocation in all three stages -- a different command would run a
# different set of tests and make the cross-stage comparison meaningless.
#
# `clean` is load-bearing: prepare.sh warms the Gradle cache by running the
# suite at image-build time, and each of the three stages starts a *fresh*
# container from that image (build_dataset.py runs run/test/fix concurrently).
# A plain `./gradlew test` in the run stage would therefore report
# `> Task :test UP-TO-DATE` and emit zero test events, leaving the baseline
# empty and every test looking like NONE -> ... in the report.
_TEST_CMD = (
    "./gradlew clean test --no-daemon --console=plain --continue "
    "--init-script /home/init.gradle"
)


class VavrJacksonImageBase(Image):
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
        # build.gradle pins sourceCompatibility 1.8 and the wrapper is Gradle
        # 8.13; the CI matrix builds and tests on temurin 8/11/17/21/23, so 17
        # is a CI-validated middle ground. Shipping the JDK in the base image
        # (instead of apt-installing it from prepare.sh) means JAVA_HOME/PATH
        # are correct for every stage, not just image build.
        return "eclipse-temurin:17-jdk"

    def image_tag(self) -> str:
        return "base-jdk17"

    def workdir(self) -> str:
        return "base-jdk17"

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

ENV CI=true \\
    GRADLE_OPTS="-Dorg.gradle.daemon=false -Dfile.encoding=UTF-8" \\
    JAVA_TOOL_OPTIONS="-Xmx2g"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class VavrJacksonImageDefault(Image):
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
        return VavrJacksonImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    # Must stay exactly `pr-<number>`: gen_report parses the instance directory
    # name as int(name[3:]), so any suffix here silently drops the instance.
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
                "init.gradle",
                _INIT_GRADLE,
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

""",
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

# Bootstrap the Gradle wrapper distribution.
#
# The wrapper fetches its own zip with a plain java.net.HttpURLConnection, which
# tries only the FIRST address a hostname resolves to. services.gradle.org
# redirects to release-assets.githubusercontent.com, which has several A records;
# where the first one refuses connections the wrapper dies with "Connection
# refused" even though the host is perfectly reachable. curl walks the remaining
# addresses, so let the wrapper try first, and on failure drop the zip into the
# exact cache slot it already computed (dists/<name>/<md5-base36-of-url>/) and
# let it unpack that on the next invocation. The bare `./gradlew --version` at
# the end has no `|| true`: if the toolchain still is not runnable, fail here
# rather than ship an image whose every stage would capture zero tests.
timeout 900 ./gradlew --version > /home/wrapper_boot.log 2>&1 || true
WRAPPER_URL="$(grep -aoE 'https://[^[:space:]]+\.zip' /home/wrapper_boot.log | head -n1)"
WRAPPER_DIR="$(find /root/.gradle/wrapper/dists -mindepth 2 -maxdepth 2 -type d 2>/dev/null | head -n1)"
if [ -n "$WRAPPER_URL" ] && [ -n "$WRAPPER_DIR" ] && [ -z "$(find "$WRAPPER_DIR" -name '*.zip' -print -quit 2>/dev/null)" ]; then
  echo "prepare.sh: wrapper could not download its distribution; retrying with curl into $WRAPPER_DIR"
  curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$WRAPPER_DIR/$(basename "$WRAPPER_URL")" "$WRAPPER_URL"
fi
./gradlew --version

# Warm the Gradle distribution + dependency caches (wrapper zip, vavr, jackson,
# junit-jupiter, assertj, jacoco) into the image so the three run stages do not
# each re-resolve them.
#
# The attempt itself is `|| true` so a flaky test or a native-toolchain wobble
# cannot fail the image build. But an *unwarmed* cache must not pass silently:
# the wrapper's distribution download is a single point of network failure, and
# without the check below Docker cheerfully reports "build success" for an image
# whose /root/.gradle is empty -- every later stage then either re-downloads the
# world or captures zero tests. So: retry, verify the cache is really populated,
# and fail loudly rather than ship a hollow image.
GRADLE_TIMEOUTS="-Dorg.gradle.internal.http.connectionTimeout=180000 -Dorg.gradle.internal.http.socketTimeout=180000"
n=0
while true; do
  n=$((n+1))
  ./gradlew test --no-daemon --console=plain --init-script /home/init.gradle $GRADLE_TIMEOUTS > /home/prepare_gradle.log 2>&1 || true
  if [ -d /root/.gradle/wrapper/dists ] && [ -n "$(find /root/.gradle/caches -name '*.jar' -print -quit 2>/dev/null)" ]; then
    break
  fi
  if [ "$n" -ge 5 ]; then
    echo "prepare.sh: Gradle cache still empty after $n attempts -- refusing to ship a hollow image" >&2
    tail -60 /home/prepare_gradle.log >&2
    exit 1
  fi
  echo "prepare.sh: warm-up attempt $n did not populate the cache, retrying in 15s..." >&2
  sleep 15
done
cat /home/prepare_gradle.log

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=_TEST_CMD),
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


@Instance.register("vavr-io", "vavr-jackson")
class VavrJackson(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VavrJacksonImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Per-test lines emitted by init.gradle's testLogging, e.g.:
        #   Gradle Test Run :test > Gradle Test Executor 3 \
        #       > io.vavr.jackson.issues.Issue149Test > itFailsOnAnOptionTry() FAILED
        #
        # Capture only the trailing "<FQCN> > <method>" pair. That deliberately
        # drops the "Gradle Test Executor <n>" segment, whose number varies
        # between the run/test/fix stages -- keeping it would give the same test
        # a different name in each stage and split it into separate report rows.
        # Anchoring the class to [\w.$]+ also means Gradle build-task lines
        # ("> Task :test FAILED") can never match and be counted as tests.
        # Primary identity source: the MSB_TEST marker emitted by init.gradle's
        # afterTest hook, which carries the FULLY-QUALIFIED class name. Gradle's
        # console events only ever print the simple class name, which cannot be
        # turned back into a source path.
        #
        #   MSB_TEST|SUCCESS|io.vavr.jackson.issues.Issue149Test|itFailsOnOptionEither()
        #     -> src/test/java/io/vavr/jackson/issues/Issue149Test.java::itFailsOnOptionEither()
        #
        # The path form is what report.py's _test_name_matches_files() expects
        # (it splits on "::"), so target-test detection matches the patch's own
        # file instead of falling back to comparing class name against basename.
        marker_re = re.compile(r"MSB_TEST\|(\w+)\|([\w.$]+)\|(.+?)\s*$")

        def _identity(fqcn: str, method: str) -> str:
            # Nested/inner classes (Outer$Inner) live in the OUTER class's file;
            # keep the inner name on the method side so identities stay unique.
            outer, _, inner = fqcn.partition("$")
            path = f"{_TEST_SOURCE_ROOT}/{outer.replace('.', '/')}.java"
            name = f"{inner}.{method}" if inner else method
            return f"{path}::{name}"

        test_re = re.compile(r"(?:^|> )([\w.$]+ > [^>]+?) (PASSED|FAILED|SKIPPED)$")

        # Some log transports chunk the stream and can break an event line right
        # before its status word, leaving a bare "PASSED" on its own line and
        # silently dropping that test. Re-attach any orphan status word to the
        # preceding line; if that line was not a test event the merged line
        # simply fails to match, so this is safe.
        lines: list[str] = []
        for raw in clean_log.splitlines():
            stripped = raw.strip()
            if stripped in ("PASSED", "FAILED", "SKIPPED") and lines:
                lines[-1] = f"{lines[-1].rstrip()} {stripped}"
            else:
                lines.append(raw)

        for line in lines:
            m = marker_re.search(line)
            if not m:
                continue
            result, fqcn, method = m.group(1), m.group(2), m.group(3)
            name = _identity(fqcn, method)
            if result == "SUCCESS":
                passed_tests.add(name)
            elif result == "FAILURE":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Fallback: an image built before init.gradle carried the afterTest hook
        # emits no markers at all. Rather than report zero tests, fall back to the
        # console events -- identities are simple-class-based, not paths, but the
        # stage is still measured.
        if not (passed_tests or failed_tests or skipped_tests):
            for line in lines:
                m = test_re.search(line.rstrip())
                if not m:
                    continue
                name, status = m.group(1).strip(), m.group(2)
                if status == "PASSED":
                    passed_tests.add(name)
                elif status == "FAILED":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A retried or re-reported test can appear twice with
        # different statuses, so collapse with FAILED > SKIPPED > PASSED.
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
