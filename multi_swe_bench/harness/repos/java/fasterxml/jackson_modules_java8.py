import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The base commit (2020-10-01) sits on the 2.12 development branch and the
# repo's own CI (.travis.yml) tests openjdk8 and openjdk11. JDK 8 is the
# project's floor and primary target, so it is what the suite is graded on.
# AdoptOpenJDK is the era vendor, the image is Ubuntu-based (Debian CA paths,
# apt available), and this tag publishes linux/amd64 AND linux/arm64
# (verified in the manifest). 8u272-b10 is the 8-line release current in
# October 2020 - pinned to the build, not a floating `8-jdk`.
JAVA_IMAGE = "adoptopenjdk:8u272-b10-jdk-hotspot"

# No Maven wrapper is committed, so Maven is installed in the base image:
# 3.6.3, the current release at the base date, from Apache's permanent archive.
MAVEN_VERSION = "3.6.3"
MAVEN_URL = (
    "https://archive.apache.org/dist/maven/maven-3/"
    f"{MAVEN_VERSION}/binaries/apache-maven-{MAVEN_VERSION}-bin.tar.gz"
)

# Both patches touch only the `datetime` module, so the reactor is scoped with
# `-pl datetime -am` (`-am` additionally builds the parent aggregator POM;
# datetime has no sibling-module dependencies). `clean` is load-bearing for the
# same reason as the other Java configs in this batch: target/ is gitignored
# and survives `git clean -qfd`, so without it a stage could run STALE .class
# files left by prepare.sh's warm run at the base commit and silently destroy
# the fail-to-pass signal.
TEST_CMD = "mvn -B -ntp clean test -pl datetime -am"

# --------------------------------------------------------------------------
# The vanished-snapshot repair (read before judging the sed below)
# --------------------------------------------------------------------------
# At the base commit the root pom declares
#     <parent>com.fasterxml.jackson:jackson-base:2.12.0-SNAPSHOT</parent>
# That snapshot lived on oss.sonatype.org and was purged years ago - verified
# empirically before this config was written: `mvn validate` at the pristine
# base commit fails with "Non-resolvable parent POM ... jackson-base:pom:
# 2.12.0-SNAPSHOT". Without a parent the build cannot even start, at any
# stage, so the instance would be dead on arrival for infrastructure reasons
# that have nothing to do with the PR.
#
# The repair substitutes the RELEASED pom of the very same line -
# jackson-base:2.12.0, published to Maven Central on 2020-11-28, eight weeks
# after the base commit. A parent POM carries build configuration and
# dependencyManagement only - no code under test - and the substitution was
# proven safe empirically: with it, the full datetime suite runs green at the
# pristine base commit (BUILD SUCCESS, every class reporting). The sed targets
# only the FIRST occurrence, which is the <parent> block; the project's own
# 2.12.0-SNAPSHOT version and the inter-module references stay untouched and
# resolve locally. Applied identically in every stage and in the warm run, so
# no stage is graded on a different build environment than any other.
POM_PARENT_FIX = (
    'sed -i "0,/<version>2.12.0-SNAPSHOT<\\/version>/'
    's//<version>2.12.0<\\/version>/" pom.xml'
)

# No compile-failure fallback on this instance - removed on QC's
# recommendation (P7/A1 of the 2026-08-26 review). This PR's test patch
# modifies EXISTING tests to assert new timezone behaviour, so the tests
# compile and fail at RUNTIME - the ideal Java f2p shape, verified in all
# three stage logs (one mvn invocation per stage, zero deletions). A
# delete-and-retry branch would therefore be a dormant hazard with no
# benefit here: the one thing it could ever do on a future rerun is erase a
# compile-failing test before it is graded. If a compilation failure does
# occur, Maven aborts loudly and parse_log records the failing class as
# FAILED from javac's own error lines - reported, never repaired.

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR): the harness streams `docker buildx` output through
# `subprocess` with `text=True` and no explicit encoding, so a Windows host
# decodes it with cp1252 and any UTF-8 byte outside that map aborts the build.
# Runtime logs are decoded as UTF-8 explicitly, so only build-time commands
# are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# Declared ONCE, in the base image only; Docker propagates ENV to the PR
# image. The timezone-serialization tests this PR is about are exactly the
# kind that a drifting platform default charset or TZ could distort, so both
# are pinned (LANG/TZ come from the enhancer's infrastructure block).
ENCODING_ENV = """ENV MAVEN_OPTS="-XX:+TieredCompilation -XX:TieredStopAtLevel=1" \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    NO_COLOR=1"""


class ImageBase(Image):
    """Per-PR base: OS + JDK 8 + Maven 3.6.3 + the repo frozen at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is
    inside it (QC item P1). A single shared `base` tag cannot make that
    promise - the first PR to build it freezes it, and every later PR silently
    inherits the wrong commit while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form: that exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite supplies the REPO_URL/BASE_COMMIT clone, the
    checkout, the history-sanitising scrub with its four integrity asserts,
    and the final CMD. Decorate that line and the enhancer stops recognising
    it, so the hardening block is silently never injected.
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

    def dependency(self) -> Union[str, "Image"]:
        return JAVA_IMAGE

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

        # adoptopenjdk ships the JDK only - no git, no curl - so one apt layer
        # adds them, and the second RUN installs the pinned Maven from
        # Apache's archive (no wrapper is committed in this repo).
        return f"""FROM {image_name}

{self.global_env}

{ENCODING_ENV}

WORKDIR /home/

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl && rm -rf /var/lib/apt/lists/* ) 2>&1 | {ASCII_FILTER}"

RUN /bin/bash -o pipefail -c "( curl -fsSL {MAVEN_URL} | tar -xz -C /opt && ln -s /opt/apache-maven-{MAVEN_VERSION}/bin/mvn /usr/local/bin/mvn && mvn --version ) 2>&1 | {ASCII_FILTER}"

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """Per-PR layer: the patches, the stage scripts, and the warmed ~/.m2."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Integrity guard. prepare.sh calls this immediately after `git reset --hard`
# and again after `git checkout <BASE_COMMIT>`, so a tree that did not actually
# come back clean aborts the BUILD instead of being baked into the image and
# silently contaminating all three graded stages.
#
# `git status --porcelain` is empty only when there is nothing modified, staged
# or untracked - deliberately stricter than `git diff --quiet`, because the
# failure this catches is usually a leftover UNTRACKED file (`git clean -qfd`
# does not remove ignored files without -x).
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
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

cd /home/{repo}
git reset --hard
# Assert the reset really produced a clean tree, so a leftover file cannot be
# baked into the image and silently inherited by all three graded stages.
bash /home/check_git_changes.sh

# The base image is frozen at ONE commit and has had its origin remote stripped
# by the hardening block, so a commit that is not already present cannot be
# resolved locally and `git fetch origin` has no remote to use. Ask GitHub for
# that exact commit by sha over the full URL. A fetch drags fresh git objects
# into an image whose history the base deliberately stripped, so the block
# below re-runs the scrub in exactly that case. On this instance the base was
# built from this very sha, so the fetch never runs.
FETCHED=0
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
    FETCHED=1
fi
git checkout {sha}
bash /home/check_git_changes.sh

if [ "$FETCHED" = "1" ]; then
    git checkout --detach {sha}
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    git repack -a -d -l --quiet
    rm -f .git/objects/info/alternates
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# Warm ~/.m2 with the parent-POM repair applied (see POM_PARENT_FIX in the
# config for the full story: the declared snapshot parent no longer exists on
# any repository, and the released 2.12.0 pom of the same line is substituted
# in every stage identically). The warm run pulls Maven's plugins and the
# whole scoped dependency tree so the graded stages never touch the network
# for jars. Outcome irrelevant to grading, hence `|| true`.
{pom_fix}
{test_cmd} || true
git reset --hard
git clean -qfd
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
                    pom_fix=POM_PARENT_FIX,
                    test_cmd=TEST_CMD,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
{pom_fix}
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    pom_fix=POM_PARENT_FIX,
                    test_cmd=TEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{pom_fix}
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    pom_fix=POM_PARENT_FIX,
                    test_cmd=TEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{pom_fix}
{test_cmd}
""".format(
                    repo=self.pr.repo,
                    pom_fix=POM_PARENT_FIX,
                    test_cmd=TEST_CMD,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

# `-o pipefail` so a failing prepare.sh still fails the build: without it the
# pipeline would report the exit status of `tr`, which always succeeds, and a
# broken image would be published as if it were good.
RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {ASCII_FILTER}"

{self.clear_env}

"""


@Instance.register("FasterXML", "jackson-modules-java8")
class JacksonModulesJava8(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # Surefire prints one summary per test class; JUnit 4 era says
        # "- in <fqcn>", newer surefire says "-- in" - the regex accepts both.
        # Class-level ids are used deliberately: surefire names individual
        # methods only when they FAIL, so method ids would exist at the test
        # stage with no matching PASS id at the fix stage. A class id carries
        # a definite status in every stage, which is what the cross-stage
        # f2p/p2p comparison needs.
        summary = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+),"
            r".*?\bin ([\w.$]+)"
        )

        # Safety net for a compile-failure ever occurring on a rerun: javac's
        # error lines name the file, and the path under src/test/java maps 1:1
        # onto the class id the summaries use.
        compile_err = re.compile(
            r"\[ERROR\]\s+\S*/src/test/java/(\S+)\.java:\[\d+,\d+\]"
        )
        for m in compile_err.finditer(log):
            failed_tests.add(m.group(1).replace("/", "."))

        for line in log.splitlines():
            m = summary.search(line)
            if not m:
                continue
            run, fail, err, skip = (int(m.group(i)) for i in range(1, 5))
            name = m.group(5).strip()
            if fail > 0 or err > 0:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif name in failed_tests:
                continue
            elif run == skip and run > 0:
                if name not in passed_tests:
                    skipped_tests.add(name)
            elif run > 0:
                skipped_tests.discard(name)
                passed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
