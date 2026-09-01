import re
import textwrap
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Top-level directories that are not buildable reactor modules. rocketmq's
# reactor is flat (client, common, broker, store, ...), so unlike dubbo there
# are no multi-module grouping parents to expand — a single path segment is
# always the module name.
_NON_MODULE_DIRS = frozenset(
    {
        ".github",
        ".git",
        ".gitignore",
        ".gitattributes",
        ".mvn",
        "docs",
        "style",
    }
)

# distribution/ is packaging only (conf files, shell scripts, assembly descriptor).
# It carries no test sources, and building it drags in the assembly plugin. When a
# patch touches only distribution/, there is nothing to test there — but it must not
# be the sole -pl target either, or surefire runs zero tests.
_NO_TEST_MODULES = frozenset({"distribution"})


def _strip_diff_prefix(path: str) -> str:
    """'b/broker/src/...' -> 'broker/src/...'"""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _extract_modules_from_patch(patch: str) -> set[str]:
    modules: set[str] = set()
    if not patch:
        return modules
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        segments = _strip_diff_prefix(parts[2]).split("/")
        if len(segments) < 2:
            # Root-level file (pom.xml, README.md, ...) — not a module.
            continue
        top = segments[0]
        if top in _NON_MODULE_DIRS:
            continue
        modules.add(top)
    return modules


def _build_pl_flag(pr: PullRequest) -> str:
    """Scope the reactor to the modules the PR actually touches.

    rocketmq has 15-18 reactor modules; a full ``mvn test`` takes 30-60 minutes
    per stage (x3 stages) and buries the relevant results among timing-sensitive
    store/broker tests that bind ports and flake. A flake in an unrelated module
    flips a test between the test-patch and fix-patch stages and is then read as
    a genuine f2p transition.

    ``-am`` (also-make) is required so the parent POM and the sibling modules the
    target depends on (common, remoting, store, ...) are built too.
    """
    all_modules = _extract_modules_from_patch(
        pr.fix_patch
    ) | _extract_modules_from_patch(pr.test_patch)
    all_modules.discard("")
    # Drop packaging-only modules, but only if something testable remains.
    testable = all_modules - _NO_TEST_MODULES
    if testable:
        all_modules = testable
    if not all_modules:
        return ""
    return "-pl " + ",".join(sorted(all_modules)) + " -am"


def _extract_test_classes(patch: str) -> list[str]:
    """Collect the simple class names of the Java test files the patch touches.

    Returns e.g. ["PlainAccessControlFlowTest"] for a patch adding
    ``acl/src/test/java/.../PlainAccessControlFlowTest.java``.
    """
    classes: set[str] = set()
    if not patch:
        return []
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        path = _strip_diff_prefix(parts[2])
        if "/src/test/java/" not in path or not path.endswith(".java"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".java")]
        # A test patch also ships shared scaffolding -- abstract bases and helper
        # classes (rocketmq: ContainerIntegrationTestBase, TransactionListenerImpl).
        # Surefire's default includes are **/Test*, **/*Test, **/*Tests,
        # **/*TestCase (plus *IT here), so naming anything else in -Dtest either
        # matches nothing or forces a non-test class to be "run". Keep only names
        # that surefire would have selected on its own.
        if not (
            name.startswith("Test")
            or name.endswith(("Test", "Tests", "TestCase", "IT"))
        ):
            continue
        # Abstract bases end in TestBase / IntegrationTestBase and cannot run.
        if name.endswith("TestBase"):
            continue
        classes.add(name)
    return sorted(classes)


def _build_test_flag(pr: PullRequest) -> str:
    """Grade only the test classes the test patch actually adds or changes.

    Without this the graded command is a whole-module ``mvn test``, and rocketmq's
    own suites are not isolated from each other -- they mutate tracked fixture
    files under ``src/test/resources`` and leave them dirty. Concretely, in the
    acl module ``PlainAccessValidatorTest`` rewrites
    ``acl/src/test/resources/conf/plain_acl.yml`` and never restores it (verified:
    ``git status`` reports it ``M`` after that class runs alone). A later class in
    the same module then reads corrupted fixtures. For PR #3927 that made the
    gold test fail in BOTH the test and fix stages -- no f2p transition, and
    ``Report.check()`` rule 3 rejected the instance. This is an upstream test
    isolation defect, not a patch defect: surefire ``reuseForks=false`` does NOT
    help, because a fresh JVM still sees the same dirty working tree.

    Restricting ``-Dtest`` to the patch's own classes sidesteps the polluters and
    grades exactly the contract the dataset cares about: the tests the PR ships.
    Verified for #3927 -- test stage ``Failures: 1``, fix stage ``Tests run: 3,
    Failures: 0``: a real fail-to-pass transition.

    ``-Dsurefire.failIfNoSpecifiedTests=false`` keeps the reactor green in the
    modules pulled in by ``-am`` that contain none of the named classes.
    """
    classes = _extract_test_classes(pr.test_patch)
    if not classes:
        return ""
    return (
        '"-Dtest=' + ",".join(classes) + '" -Dsurefire.failIfNoSpecifiedTests=false'
    )


# Skips that keep the run focused on tests only. rocketmq binds all of these to
# the default lifecycle, and each one can fail the build *before* surefire runs:
#  * apache-rat  : license-header audit. A test.patch / fix.patch hunk is not
#                  guaranteed to carry an ASF header, so rat would abort the
#                  test and fix stages and produce 0/0/0.
#  * checkstyle  : style audit, same failure mode, no test signal.
#  * jacoco/enforcer/javadoc/clirr/versions/gpg/license: not test signal.
_SKIP_FLAGS = (
    "-Drat.skip=true -Dcheckstyle.skip=true -Dcheckstyle_unix.skip=true "
    "-Djacoco.skip=true -Denforcer.skip=true -Dmaven.javadoc.skip=true "
    "-Dclirr.skip=true -Dversions.skip=true "
    "-Dlicense.skip=true -Dgpg.skip=true"
)

# rocketmq's root pom sets <skipAfterFailureCount>1</skipAfterFailureCount>, which
# makes surefire ABORT the whole run at the first test failure. The test-patch
# stage is *expected* to fail, so with the pom default every test after the first
# failure is never reported: parse_log sees a truncated log and the f2p set becomes
# an arbitrary function of module ordering. Setting it to 0 disables the early exit
# and lets every test report its own result in all three stages.
_SUREFIRE_FLAGS = "-Dsurefire.skipAfterFailureCount=0 -Dsurefire.useFile=false"

# -B  : batch mode, no ANSI colour, stable machine-readable output
# -ntp: no transfer-progress spam (keeps the captured log parseable)
# -fn : fail-never. Test failures are the expected outcome of the test-patch
#       stage; -fn makes Maven exit 0 on them while still exiting non-zero when
#       the reactor itself cannot be built. That is what lets the run scripts use
#       `set -eo pipefail` with no `|| true` on the test command.
_MVN_BASE = (
    f"mvn -B -ntp test -fn "
    f"{_SUREFIRE_FLAGS} -Dmaven.test.skip=false -DfailIfNoTests=false "
    f"{_SKIP_FLAGS}"
)


def _mvn_test_command(pr: PullRequest) -> str:
    parts = [_MVN_BASE]
    pl_flag = _build_pl_flag(pr)
    if pl_flag:
        parts.append(pl_flag)
    test_flag = _build_test_flag(pr)
    if test_flag:
        parts.append(test_flag)
    return " ".join(parts)


def _mvn_warmup_command(pr: PullRequest) -> str:
    """Dependency/plugin warm-up for the image build: resolve + compile, no run."""
    pl_flag = _build_pl_flag(pr)
    base = f"mvn -B -ntp clean test-compile -fn -DskipTests {_SKIP_FLAGS}"
    return f"{base} {pl_flag}" if pl_flag else base


class RocketmqImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        # ONE shared base for every PR in this repo. Images are deduplicated on
        # image_full_name() (Image.__hash__/__eq__), so a constant tag collapses
        # all PRs onto a single build of the heavy JDK+Maven+clone layer instead
        # of one per PR.
        #
        # The base is therefore pinned to whichever PR's BASE_COMMIT won the
        # dedup race, and the enhancer's history scrub prunes everything else
        # (verified: only that one commit survives; `git rev-list --all --count`
        # = 2004, all refs and remotes deleted). Each PR layer's prepare.sh
        # re-pins the tree itself -- it fetches its own BASE_COMMIT back from
        # origin before checking it out, so a shared base stays correct for
        # every PR. See prepare.sh below.
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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

# Two apt calls, deliberately -- do not collapse into one.
# Ubuntu's `maven` declares `Depends: default-jre-headless | <java7-runtime-headless>`.
# Resolving `maven` on a JDK-less image takes the first alternative and pulls
# openjdk-11-jre-headless ALONGSIDE openjdk-8-jdk; on arm64 the JDK 11 postinst
# then fails to configure, dpkg returns 1 and the layer aborts (apt exit 100).
# Installing JDK 8 first registers it as a java7-runtime-headless provider, so
# the second call satisfies maven's alternative with the JDK already present and
# never fetches JDK 11. Verified live on ubuntu:22.04/arm64: 0 openjdk-11
# packages, `java -version` = 1.8.0_502, `mvn -v` = Apache Maven 3.6.3.
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-8-jdk \
    && apt-get install -y --no-install-recommends \
    git ca-certificates curl maven \
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class RocketmqImageDefault(Image):
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
        return RocketmqImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        mvn_cmd = _mvn_test_command(self.pr)
        mvn_warmup = _mvn_warmup_command(self.pr)
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

export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

# The base image is shared by every PR of this repo, so it is pinned to one
# BASE_COMMIT and the enhancer's history scrub pruned every other commit
# (all refs and remotes are deleted there). Fetch this PR's own base commit
# back before checking it out. No-op when the commit is already present.
if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --no-tags --depth 1 https://github.com/{org}/{repo}.git {sha}
    git checkout --detach FETCH_HEAD
else
    git checkout --detach {sha}
fi
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{sha}"

{mvn_warmup} || true
""".format(
                    org=self.pr.org,
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    mvn_warmup=mvn_warmup,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.odg' --exclude='*.swp' --exclude='*.class' /home/test.patch
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which javac))))
export PATH="$JAVA_HOME/bin:$PATH"
export MAVEN_OPTS="-Xmx4g -XX:+UseParallelGC"

cd /home/{repo}
git apply --whitespace=nowarn --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.ico' --exclude='*.bmp' --exclude='*.odg' --exclude='*.swp' --exclude='*.class' /home/test.patch /home/fix.patch
{mvn_cmd}
""".format(repo=self.pr.repo, mvn_cmd=mvn_cmd),
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
                RUN mkdir -p ~/.m2 && \\
                    if [ ! -f ~/.m2/settings.xml ]; then \\
                        echo '<?xml version="1.0" encoding="UTF-8"?>' > ~/.m2/settings.xml && \\
                        echo '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"' >> ~/.m2/settings.xml && \\
                        echo '          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"' >> ~/.m2/settings.xml && \\
                        echo '          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.0.0 https://maven.apache.org/xsd/settings-1.0.0.xsd">' >> ~/.m2/settings.xml && \\
                        echo '</settings>' >> ~/.m2/settings.xml; \\
                    fi && \\
                    sed -i '$d' ~/.m2/settings.xml && \\
                    echo '<proxies>' >> ~/.m2/settings.xml && \\
                    echo '    <proxy>' >> ~/.m2/settings.xml && \\
                    echo '        <id>example-proxy</id>' >> ~/.m2/settings.xml && \\
                    echo '        <active>true</active>' >> ~/.m2/settings.xml && \\
                    echo '        <protocol>http</protocol>' >> ~/.m2/settings.xml && \\
                    echo '        <host>{proxy_host}</host>' >> ~/.m2/settings.xml && \\
                    echo '        <port>{proxy_port}</port>' >> ~/.m2/settings.xml && \\
                    echo '        <username></username>' >> ~/.m2/settings.xml && \\
                    echo '        <password></password>' >> ~/.m2/settings.xml && \\
                    echo '        <nonProxyHosts></nonProxyHosts>' >> ~/.m2/settings.xml && \\
                    echo '    </proxy>' >> ~/.m2/settings.xml && \\
                    echo '</proxies>' >> ~/.m2/settings.xml && \\
                    echo '</settings>' >> ~/.m2/settings.xml
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN sed -i '/<proxies>/,/<\\/proxies>/d' ~/.m2/settings.xml
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


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")

# "[INFO] Running org.apache.rocketmq.acl.plain.PlainAccessControlFlowTest"
_RUNNING_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Running\s+(\S+)\s*$")

# "Tests run: 4, Failures: 1, Errors: 0, Skipped: 0"
_SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)"
)

# Surefire 3.x appends the owning class: "... 0.12 s -- in com.foo.BarTest"
# (older 3.0 milestones used a single dash: "- in com.foo.BarTest")
_IN_CLASS_RE = re.compile(r"(?:--|-)\s+in\s+(\S+)\s*$")

# "[INFO] Results:" starts the aggregate section, whose "Tests run:" line is a
# module-wide total and must NOT be attributed to the last-seen class.
_RESULTS_RE = re.compile(r"^(?:\[[A-Z]+\]\s*)?Results:\s*$")

_FAILURE_MARKER_RE = re.compile(r"<<<\s*(FAILURE|ERROR)!")


@Instance.register("apache", "rocketmq")
class Rocketmq(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RocketmqImageDefault(self.pr, self._config)

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
        """Attribute every surefire summary line to its owning test class.

        Surefire only enumerates individual methods when they fail, so the
        stable unit that exists in every stage is the fully qualified test
        *class*. Class FQNs carry no timing/count metadata, so the same class
        yields an identical name in the run / test / fix stages.

        The scan is a single linear pass — no multi-line regex with nested
        quantifiers, which would backtrack badly on a multi-megabyte reactor log.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        current_class: str | None = None
        for line in clean_log.split("\n"):
            running = _RUNNING_RE.match(line.strip())
            if running:
                current_class = running.group(1)
                continue

            if _RESULTS_RE.match(line.strip()):
                # Aggregate section — nothing after this belongs to a class
                # unless Surefire re-states it via "-- in <class>".
                current_class = None
                continue

            summary = _SUMMARY_RE.search(line)
            if not summary:
                continue

            in_class = _IN_CLASS_RE.search(line)
            test_name = in_class.group(1) if in_class else current_class
            # A "Tests run:" line with neither an "-- in <class>" suffix nor a
            # preceding "Running <class>" is a module/reactor total. Ignore it.
            current_class = None
            if not test_name:
                continue

            tests_run = int(summary.group(1))
            failures = int(summary.group(2))
            errors = int(summary.group(3))
            skipped = int(summary.group(4))

            if failures > 0 or errors > 0 or _FAILURE_MARKER_RE.search(line):
                failed_tests.add(test_name)
            elif tests_run == 0:
                continue
            elif skipped == tests_run:
                skipped_tests.add(test_name)
            else:
                passed_tests.add(test_name)

        # TestResult.__post_init__ requires the three sets to be pairwise
        # disjoint. A class can legitimately show up more than once (e.g. built
        # under more than one reactor module), so resolve by severity:
        # failed > passed > skipped.
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
