import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Java 17 is what this era's CI runs (.github/workflows/maven.yml matrix is
# [17, 21]; release.yml pins 17). Temurin is the mainstream OpenJDK build and
# publishes this tag for both linux/amd64 and linux/arm64, which is what makes
# a multi-arch manifest possible for this repo - the JVM hides the CPU from the
# code under test entirely.
#
# The tag is pinned to the full version, not a rolling `17-jdk`, so a rebuild
# next year produces the same JDK. 17.0.15 is the current 17-line release at
# the base commit's date (2025-06-04).
JAVA_IMAGE = "eclipse-temurin:17.0.15_6-jdk"

# The repo builds with its own Maven wrapper (mvnw, pinned to Maven 3.9.9 by
# .mvn/wrapper/maven-wrapper.properties), exactly as CI does - so no Maven is
# installed in the image; the wrapper downloads its pinned distribution into
# ~/.m2 during prepare.sh and every later run reuses it.
#
# The test patch and the fix patch both touch only the `metrics-core` module,
# so the reactor is scoped with `-pl metrics-core -am` instead of building all
# 38 modules: `-am` additionally builds what metrics-core depends on (just the
# parent POM - metrics-core has no internal module dependencies, verified in
# its pom.xml), keeping a graded stage at minutes instead of an hour while
# still running every test class in the module for p2p signal.
#
# `-B` (batch) kills color and progress animations; `-ntp` silences the
# transfer-progress spam that would otherwise dominate the log.
#
# `clean` is deliberate and load-bearing. Every stage starts by wiping target/,
# because target/ is gitignored and survives `git clean -qfd` - so without the
# clean, a stage whose test-compile fails would let surefire pick up STALE
# .class files left by prepare.sh's warm run at the BASE commit. The stale
# MetricNameTest.class (base version) passes, which would silently destroy the
# fail-to-pass signal. Wiping target/ makes every stage compile what the
# patched tree actually contains.
TEST_CMD = "./mvnw -B -ntp clean test -pl metrics-core -am"

# The graded block every stage script runs - identical in all three (P7); only
# the patches applied beforehand differ.
#
# Why two attempts: the gold test calls MetricName.parse(), which does not
# exist at the base commit, so at the TEST stage javac fails and Maven aborts
# before surefire launches - and unlike pytest, Maven has no
# --continue-on-collection-errors: one uncompilable test file silences the
# whole module, and every other class would be recorded as NONE ("test cases
# are not being captured"). So:
#
#   attempt 1  runs the real command. If it compiles (run and fix stages),
#              surefire runs everything and attempt 2 never happens.
#   fallback   only when attempt 1 hit a COMPILATION ERROR: the javac error
#              lines (which parse_log has already turned into a FAIL for the
#              class that owns them) name the uncompilable files; those files
#              are removed from the throwaway working tree and the SAME
#              command reruns, so the remaining classes report real results
#              instead of NONE. The tree is reset at the start of every stage,
#              so the removal never outlives the stage.
#
# This is the Maven equivalent of the pytest config's
# --continue-on-collection-errors: the broken class stays FAILED (from the
# compile errors), and the other 36 classes contribute genuine
# PASS/FAIL observations at every stage.
STAGE_BLOCK = """{test_cmd} 2>&1 | tee /tmp/mvn-attempt1.log
if grep -q "COMPILATION ERROR" /tmp/mvn-attempt1.log; then
    grep -oE "/home/{repo}/[^ :]+/src/test/java/[^ :]+\\.java" /tmp/mvn-attempt1.log \\
        | sort -u | while read -r f; do rm -f "$f"; done
    {test_cmd}
fi"""

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR). The harness streams `docker buildx` output through
# `subprocess` with `text=True` and no explicit encoding, so a Windows host
# decodes it with cp1252 - and any UTF-8 byte outside that map aborts the build
# before a single layer is produced. Runtime logs are unaffected (the harness
# decodes those as UTF-8 explicitly), so only build-time commands are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# Declared ONCE, in the base image only; Docker propagates ENV to the PR image.
# MAVEN_OPTS mirrors the repo's own CI (JAVA_OPTS in maven.yml): capping the
# JIT at tier 1 trades peak speed for much faster JVM startup, which is the
# right trade for short-lived test forks.
ENCODING_ENV = """ENV MAVEN_OPTS="-XX:+TieredCompilation -XX:TieredStopAtLevel=1" \\
    JAVA_TOOL_OPTIONS="-Dfile.encoding=UTF-8" \\
    NO_COLOR=1"""


class ImageBase(Image):
    """Per-PR base: OS + JDK + the cloned repo, frozen at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is inside
    it (QC item P1). A single shared `base` tag cannot make that promise - the
    first PR to build it freezes it, and every later PR silently inherits the
    wrong commit while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form: that exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite supplies the REPO_URL/BASE_COMMIT clone, the
    checkout, the history-sanitising scrub with its four integrity asserts, and
    the final CMD. Decorate that line and the enhancer stops recognising it, so
    the hardening block is silently never injected.
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

        # eclipse-temurin ships the JDK only - no git - so one apt layer adds
        # it. ca-certificates is already present (the image is Ubuntu-based and
        # temurin needs TLS itself), but naming it is free and makes the TLS
        # story explicit for QC item D10.
        return f"""FROM {image_name}

{self.global_env}

{ENCODING_ENV}

WORKDIR /home/

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* ) 2>&1 | {ASCII_FILTER}"

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

# Warm run: the Maven wrapper downloads its pinned 3.9.9 distribution and every
# dependency of the scoped reactor into ~/.m2, and the compiler fills target/.
# All of that lives OUTSIDE the working tree (~/.m2) or in gitignored paths
# (target/), so the graded stages start warm without the tree being dirty.
# The outcome is irrelevant to grading, hence `|| true`.
{test_cmd} || true
git reset --hard
git clean -qfd
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
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
{stage_block}
""".format(
                    repo=self.pr.repo,
                    stage_block=STAGE_BLOCK.format(test_cmd=TEST_CMD, repo=self.pr.repo),
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
{stage_block}
""".format(
                    repo=self.pr.repo,
                    stage_block=STAGE_BLOCK.format(test_cmd=TEST_CMD, repo=self.pr.repo),
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
{stage_block}
""".format(
                    repo=self.pr.repo,
                    stage_block=STAGE_BLOCK.format(test_cmd=TEST_CMD, repo=self.pr.repo),
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


@Instance.register("dropwizard", "metrics")
class Metrics(Instance):
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

        # Surefire prints one summary line per test class:
        #   [INFO] Tests run: 30, Failures: 0, Errors: 0, Skipped: 1,
        #          Time elapsed: 0.42 s -- in io.dropwizard.metrics5.MeterTest
        # (older surefire says "- in", 3.x says "-- in"; both are matched).
        # Class-level ids are used deliberately: surefire names individual
        # methods only when they FAIL, so method-level ids would exist at the
        # test stage but have no matching PASS id at the fix stage. A class id
        # is present in every stage with a definite status, which is what the
        # cross-stage f2p/p2p comparison needs.
        summary = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+),"
            r".*?\bin ([\w.$]+)"
        )

        # The gold test calls MetricName.parse(), which does not exist at the
        # base commit - so at the TEST stage the module fails at
        # test-COMPILATION, surefire never launches, and not one summary line
        # prints. Without this clause the whole stage would read as "no
        # results" and the compile-broken test class could never be credited
        # as fail-to-pass. javac's error lines name the file:
        #   [ERROR] /home/metrics/.../src/test/java/io/dropwizard/metrics5/
        #           MetricNameTest.java:[57,31] cannot find symbol
        # Each such file maps 1:1 onto the class id the surefire summaries use
        # (path under src/test/java, dots for slashes), so a class that fails
        # to compile is recorded as FAILED under exactly the id it PASSES
        # under in the other stages.
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
