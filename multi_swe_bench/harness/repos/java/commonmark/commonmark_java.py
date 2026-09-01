"""commonmark/commonmark-java - multi-module Maven library, JDK 11 build / Java 7 target.

Every value below was read off the repo and its resolved artifacts at base commit
27f487f0 (PR #161), not inferred from the config's own shape:

  pom.xml    parent  com.atlassian.pom:central-pom:5.0.13
             maven-compiler-plugin 3.7.0   source 7 / target 7
             maven-surefire-plugin 2.22.1
             junit 4.12                    -> JUnit 4, NOT Jupiter
             jacoco 0.7.9                  -> `coverage` PROFILE only, not the default build
             modules: commonmark, commonmark-ext-autolink,
                      commonmark-ext-gfm-strikethrough, commonmark-ext-gfm-tables,
                      commonmark-ext-heading-anchor, commonmark-ext-ins,
                      commonmark-ext-yaml-front-matter, commonmark-integration-test,
                      commonmark-test-util
             36 test classes across the reactor (24 of them in `commonmark`)
  .travis.yml  jdk: oraclejdk8 AND openjdk11, script `mvn test -Dsurefire.useFile=false`
  apt          maven 3.6.3, openjdk 11 on ubuntu:22.04

Four things are worth knowing before changing anything here.

1. The Atlassian parent POM resolves from Maven Central. `com.atlassian.pom:central-pom:5.0.13`
   reads like it needs packages.atlassian.com, and the parent does name that host - but only
   inside <distributionManagement>, which is a DEPLOY target, not a resolution source. The
   artifact itself is on Central (fetched and confirmed). No mirror, no extra <repository>, and
   no ~/.m2/settings.xml is required, so none is added.

2. JDK 11, not 8 and not 17. `<source>/<target>` are 7, which JDK 8 supports natively and which
   JDK 20 removed outright - so the safe window is 8..17. Travis is what settles it inside that
   window: this project's own CI ran `mvn test` green on openjdk11 (that leg is NOT in
   allow_failures; only the android leg is), which covers maven-compiler-plugin 3.7.0 and the
   JMH annotation processor that `commonmark` pulls into test-compile. 11 is also what Ubuntu
   22.04's `maven` package depends on, so there is exactly one JDK in the image and no
   alternatives race of the kind a 17-plus-maven install creates.

3. THE TEST STAGE COMPILES NOTHING, AND THAT IS THE CORRECT OUTCOME. test_patch adds three
   methods to HtmlRendererTest that call `HtmlRenderer.builder().sanitizeUrls(...)` and
   `new DefaultUrlSanitizer()` - API that fix_patch is what INTRODUCES. With the test patch
   alone, `commonmark`'s test-compile fails; under -fae every other module depends on
   `commonmark`, so the reactor skips them and no surefire XML is written anywhere. Expected:

     run   36 test classes                     BUILD SUCCESS
     test  0  (test-compile fails in commonmark, rest SKIPPED)
     fix   36 test classes + 3 new methods     BUILD SUCCESS

   report.py's classifier is baseline-first and reads this correctly: a pre-existing test is
   run=PASS / test=NONE / fix=PASS, which it reclassifies to p2p rather than crediting as a
   fix; the three new methods are run=NONE / test=NONE / fix=PASS and are attributed to
   test_patch's added lines, i.e. n2p. This is an n2p instance with zero f2p, and it qualifies
   on `len(f2p) > 0 or len(n2p) > 0`. Do not "fix" the empty test stage by moving the API
   additions into test_patch - that would hand the solution to the agent.

4. Because of (3), per-METHOD test ids are load-bearing here, not a nicety. Surefire's console
   names an individual method only when it FAILS; passing methods appear nowhere but the XML
   under target/surefire-reports/. Grading off the console alone collapses to one entry per
   test CLASS, and at class granularity `org.commonmark.test.HtmlRendererTest` is
   run=PASS / test=NONE / fix=PASS - p2p. The three new methods would vanish into a class that
   already existed, the instance would report zero f2p AND zero n2p, and it would be discarded
   by the selection rule while still passing Report.check(). The XML emitter below is what
   keeps them visible.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `clean` rather than an incremental build: prepare.sh warms ~/.m2, which is the
# expensive part (network); target/ is cheap to rebuild and a stale class file
# shared between the three graded stages would make them non-independent.
#
# -Dmaven.test.failure.ignore=true and -fae exist for the same reason: f2p/n2p are
# derived by COMPARING the three stages, so a stage that aborts early reports a
# truncated suite. Every test that never ran then looks like it disappeared,
# inventing transitions that never happened and hiding the real ones. Grading reads
# parse_log, never the exit code, so suppressing the non-zero exit costs nothing.
#
# Neither flag rescues the test stage here - a test-COMPILE failure is not a test
# failure and -fae only skips dependents (see docstring point 3). They are still
# correct: they are what stops one flaky method in one module from truncating the
# other eight in the run and fix stages.
MVN_TEST = (
    "mvn -B -ntp clean test -Dstyle.color=never -Dmaven.test.failure.ignore=true -fae"
)

# Exported by every script that invokes Maven, rather than declared as ENV in the
# base image, so the generated Dockerfile carries exactly ONE ENV instruction -
# the one DockerfileEnhancer injects. Nothing is lost by moving them here: the
# base image build itself never runs Maven (only apt and git), so these variables
# have no consumer until a script runs.
#
#   JAVA_HOME  Maven falls back to `which java` when this is unset, which resolves
#              correctly - but only via the alternatives link. Setting it makes the
#              graded stages independent of alternatives state entirely.
#   PATH       Puts the JDK's bin ahead of anything the base image may add later.
#   LC_ALL     The enhancer sets LANG but not LC_ALL, which leaves LC_COLLATE at the
#              C default; Surefire orders report output by locale, and this repo's
#              own tests compare rendered HTML strings.
#   MAVEN_OPTS Surefire forks a JVM per module. Pinning the heap removes the
#              variance between an amd64 and an arm64 runner sizing the default
#              differently, and covers the Java heap item in Check 2C.
#   CI         Nothing in THIS build reads it - Maven, Surefire and JUnit 4 all
#              ignore it. It is exported anyway because it is the tree-wide
#              baseline for a graded stage, and because a plugin added by a later
#              PR of this repo would silently pick up interactive behaviour
#              without it.
SHELL_ENV = """\
export CI=true
export JAVA_HOME=/usr/lib/jvm/java-11
export PATH="$JAVA_HOME/bin:$PATH"
export LC_ALL=C.UTF-8
export MAVEN_OPTS="-Xmx2g -Djava.awt.headless=true\""""


# Emitted after every graded Maven run and parsed by parse_log. See docstring
# point 4 for why per-method ids are required for this PR specifically.
#
# This reads whatever XML is on disk, so it is only correct if the tree holds
# nothing but the stage that just ran. `mvn clean` is not sufficient on its own -
# it only touches modules in the reactor, and the test stage's reactor is mostly
# SKIPPED. The graded scripts delete every surefire-reports directory before Maven
# runs, and prepare.sh finishes with `git clean -fdx`; both are load-bearing.
#
# Written in Python rather than shell because a <testcase> is self-closing when it
# passes but carries a child element when it fails or is skipped, and failure
# bodies contain stack traces with angle brackets that a grep/sed parser would
# choke on. ElementTree is stdlib, so this adds no dependency.
SUREFIRE_REPORT_PY = r"""#!/usr/bin/env python3
import glob
import os
import sys
import xml.etree.ElementTree as ET


def status_of(testcase):
    for child in testcase:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("failure", "error"):
            return "FAIL"
        if tag == "skipped":
            return "SKIP"
    return "PASS"


# Repo-relative path of the file declaring `classname`, or None.
#
# Surefire writes <module>/target/surefire-reports/TEST-<FQCN>.xml, so the module
# directory is the report path's great-grandparent. That removes any guessing
# about which module a class came from in a multi-module reactor - and this
# reactor has nine, several of which declare same-named helper classes.
def source_path(root, xml_path, classname):
    module_dir = os.path.dirname(os.path.dirname(os.path.dirname(xml_path)))
    # Outer$Inner and Outer$1 both live in Outer.java.
    top_level = classname.split("$", 1)[0]
    relative = top_level.replace(".", os.sep) + ".java"

    candidate = os.path.join(module_dir, "src", "test", "java", relative)
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, root)

    # Non-standard source root (a build-helper extra root, a generated suite).
    # Fall back to locating the file by name in the module.
    basename = top_level.rsplit(".", 1)[-1] + ".java"
    for dirpath, _dirnames, filenames in os.walk(os.path.join(module_dir, "src")):
        if basename in filenames:
            return os.path.relpath(os.path.join(dirpath, basename), root)
    return None


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    pattern = os.path.join(root, "**", "target", "surefire-reports", "TEST-*.xml")
    for xml_path in sorted(glob.glob(pattern, recursive=True)):
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            # A run killed mid-write leaves truncated XML. Skipping it keeps the
            # rest of the suite reportable; the class simply falls back to the
            # console-derived result.
            continue
        for testcase in tree.getroot().iter("testcase"):
            classname = testcase.get("classname") or ""
            name = testcase.get("name") or ""
            if not classname or not name:
                continue
            path = source_path(root, xml_path, classname)
            # `path::name` is how every other language in this dataset identifies a
            # test. It also gives Report TWO independent ways to attribute an n2p
            # test: _candidate_identifiers splits on `::` to recover the method
            # name to look for in test_patch's added lines, AND
            # _test_name_matches_files compares the path half against
            # test_patch_files - which for this PR holds
            # commonmark/src/test/java/org/commonmark/test/HtmlRendererTest.java
            # verbatim. The FQCN form is a last resort for a class whose source
            # cannot be located; it keeps the test reportable instead of dropping
            # it.
            if path:
                test_id = "{0}::{1}".format(path, name)
            else:
                test_id = "{0}#{1}".format(classname, name)
            print("SUREFIRE_TESTCASE {0} {1}".format(status_of(testcase), test_id))


if __name__ == "__main__":
    main()
"""


class CommonmarkJavaImageBase(Image):
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
        # Per-PR, not a shared "base": DockerfileEnhancer rewrites the clone step
        # below into `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` plus a
        # hardening block that detaches at that one commit and deletes every other
        # ref. A shared tag would let whichever PR built first pin the commit for
        # all the others.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        # This image emits NO ENV instruction of its own, so the generated
        # Dockerfile contains exactly one - the block DockerfileEnhancer injects,
        # which already supplies DEBIAN_FRONTEND, LANG and TZ. The JDK variables
        # the enhancer does not supply live in SHELL_ENV, which every
        # Maven-invoking script exports; nothing here reads them, because this
        # build runs only apt and git.
        #
        # apt: python3 is required by surefire_report.py, not by the project.
        # ca-certificates-java is what seeds the JKS truststore, without which
        # Maven cannot complete a TLS handshake with Central - it survives
        # --no-install-recommends because it is a *Depends* of
        # openjdk-11-jre-headless on Ubuntu 22.04, not a Recommends.
        #
        # JDK pin: jammy's `maven` package depends on default-jre-headless, which
        # IS openjdk-11 here, so unlike a 17-based image there is only one JDK
        # present and no alternatives contention. `--set` is still applied so the
        # choice is declared rather than inherited from Debian's priority ordering,
        # which a rebuild that adds another JDK could reorder. The path is resolved
        # at build time rather than written literally because the pipeline builds
        # linux/amd64 AND linux/arm64 from this one Dockerfile and Ubuntu's JVM
        # directory carries the architecture in its name (java-11-openjdk-amd64 vs
        # -arm64); a hardcoded path would silently break one leg. The stable
        # arch-independent symlink made here is what SHELL_ENV points JAVA_HOME at.
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git python3 ca-certificates openjdk-11-jdk maven \\
    && rm -rf /var/lib/apt/lists/*

RUN JDK="/usr/lib/jvm/java-11-openjdk-$(dpkg --print-architecture)" \\
    && ln -sfn "$JDK" /usr/lib/jvm/java-11 \\
    && test -x /usr/lib/jvm/java-11/bin/java \\
    && for tool in java javac jar javadoc; do \\
           update-alternatives --set "$tool" "$JDK/bin/$tool"; \\
       done \\
    && test "$(readlink -f "$(command -v java)")" = "$JDK/bin/java"

{code}

{self.clear_env}

"""


class CommonmarkJavaImageDefault(Image):
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
        return CommonmarkJavaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "surefire_report.py", SUREFIRE_REPORT_PY),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

{shell_env}

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree rather than assuming it did. A
# stray modified file would flow into all three graded stages and corrupt the
# comparison with nothing in the log to explain why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Warm ~/.m2 into this image layer so the graded stages neither pay for the
# download nor depend on the network.
#
# ONE pass, unlike the two-pass warm-up some Maven configs in this tree use. That
# shape exists for a fix patch that adds a module or a dependency, so pass 1 can
# never see it. This fix patch adds five files under
# commonmark/src/main/java/.../renderer/html/ and touches no POM, so the base
# commit's reactor already resolves every artifact all three stages need.
#
# `timeout 1800` is NOT belt-and-braces on top of `|| true`. `|| true` handles a
# command that FAILS; a command that HANGS never returns, so it never reaches `||`
# at all - and Docker has no per-step timeout, so nothing else would break the
# deadlock. Maven blocking on a half-dead Central connection is exactly that shape.
#
# `|| true` still matters on its own: a build failure at the base commit is a
# legitimate state for some PRs and must not fail the image build. The verdict is
# recorded so a hollow image is DETECTABLE afterwards.
# Inspect with: docker run <image> cat /home/.warm_status
if timeout 1800 {mvn} > /tmp/warm.log 2>&1; then
  echo "warm-up base: OK" >> /home/.warm_status
else
  echo "warm-up base: INCOMPLETE (exit $?)" >> /home/.warm_status
  tail -20 /tmp/warm.log || true
fi
cat /home/.warm_status

# Back to pristine so the graded stages apply their own patches cleanly.
#
# `-x` is REQUIRED, not tidiness. Without it git clean skips ignored files, and
# `target/` is ignored - so every module's target/surefire-reports/ from the
# warm-up would be baked into the image, and the emitter in each graded stage
# would read those orphans as though the stage had produced them. All three
# stages then report an identical suite, every f2p/n2p/p2p bucket comes out
# EMPTY, and the instance still reports valid: silently worthless.
#
# Nothing of value is discarded: the expensive warm-up artefact is ~/.m2, which
# lives outside the repo, and every graded stage runs `mvn clean test` anyway.
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
""".format(pr=self.pr, mvn=MVN_TEST, shell_env=SHELL_ENV),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
# Defence in depth against the orphan-report failure described in prepare.sh:
# `mvn clean` only cleans modules in the reactor, and this repo's test stage
# leaves most of the reactor SKIPPED. Removing the directories first means the
# emitter can only ever see what this stage just ran.
find . -type d -name surefire-reports -prune -exec rm -rf {{}} +

mvn_exit=0
{mvn} || mvn_exit=$?
python3 /home/surefire_report.py /home/{pr.repo} || true
exit "$mvn_exit"
""".format(pr=self.pr, mvn=MVN_TEST, shell_env=SHELL_ENV),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
find . -type d -name surefire-reports -prune -exec rm -rf {{}} +

mvn_exit=0
{mvn} || mvn_exit=$?
python3 /home/surefire_report.py /home/{pr.repo} || true
exit "$mvn_exit"
""".format(pr=self.pr, mvn=MVN_TEST, shell_env=SHELL_ENV),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{shell_env}

cd /home/{pr.repo}
# test.patch first, then fix.patch - separate invocations so a failure names
# which one failed. They touch disjoint trees here (test.patch only
# commonmark/src/test/, fix.patch only commonmark/src/main/), so neither ordering
# conflicts, but the order is still the graded contract.
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
find . -type d -name surefire-reports -prune -exec rm -rf {{}} +

mvn_exit=0
{mvn} || mvn_exit=$?
python3 /home/surefire_report.py /home/{pr.repo} || true
exit "$mvn_exit"
""".format(pr=self.pr, mvn=MVN_TEST, shell_env=SHELL_ENV),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Surefire's per-class summary:
#
#   [INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.036 s - in org.commonmark.test.HtmlRendererTest
#
# The trailing ` - in <FQCN>` is required by the pattern on purpose. Surefire also
# prints a per-module recap with the identical prefix and NO class name:
#
#   [INFO] Tests run: 21, Failures: 0, Errors: 0, Skipped: 0
#
# Matching that would add one phantom "test" per module named after nothing, and
# its counts would shift whenever a real test changed - manufacturing f2p/p2p
# transitions out of a summary line. Note also that `Time elapsed` is NOT captured
# into the name: it changes between stages, and a name carrying it would make one
# test read as three (Check 4B).
_CLASS_SUMMARY_RE = re.compile(
    r"Tests run:\s*(?P<total>\d+),\s*"
    r"Failures:\s*(?P<failures>\d+),\s*"
    r"Errors:\s*(?P<errors>\d+),\s*"
    r"Skipped:\s*(?P<skipped>\d+)"
    r".*?\s-\s+in\s+(?P<cls>[\w.$]+)\s*$"
)

# `[INFO] Running <FQCN>` is printed BEFORE the class executes. Every class that
# finishes also prints a summary; one that does not finish (fork crash, OOM kill,
# JVM abort) prints only this. Tracking it is what stops a crashed class from
# vanishing from the report instead of being recorded as failed - a vanished p2p
# test reads as "never existed", which is precisely the silent corruption the
# three-stage comparison cannot otherwise detect.
_RUNNING_RE = re.compile(r"Running\s+(?P<cls>[\w.$]+)\s*$")

# Emitted by surefire_report.py, one line per test METHOD:
#
#   SUREFIRE_TESTCASE PASS commonmark/src/test/java/org/commonmark/test/HtmlRendererTest.java::attributeEscaping
_TESTCASE_RE = re.compile(
    r"^SUREFIRE_TESTCASE\s+(?P<status>PASS|FAIL|SKIP)\s+(?P<name>\S+)$"
)


def _parse_testcase_lines(lines: list[str]) -> TestResult | None:
    """Per-METHOD results from surefire_report.py, or None if it did not run."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    seen = False

    for line in lines:
        match = _TESTCASE_RE.match(line)
        if not match:
            continue
        seen = True
        name = match.group("name")
        status = match.group("status")
        # Failure wins over any other verdict for the same name. A method can be
        # reported twice when a class runs under two build variants; treating the
        # pair as passed would hide the failing one.
        if status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status == "SKIP":
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    if not seen:
        return None

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


def parse_maven_surefire_log(test_log: str) -> TestResult:
    """Parse a graded stage's output into per-METHOD results.

    Two sources, in priority order:

    1. `SUREFIRE_TESTCASE` lines from surefire_report.py, which reads the XML
       reports and so names every method including the passing ones. This is the
       normal path and the one docstring point 4 depends on.

    2. The Maven console, at per-CLASS granularity, when no such line is present -
       the reactor died before Surefire wrote any XML, the emitter itself failed,
       or an older image without it is being re-graded. Coarser, but it keeps a
       stage reportable instead of silently empty, and an empty stage would
       manufacture f2p/p2p transitions against the other two.
    """
    lines = _ANSI_RE.sub("", test_log).splitlines()

    from_xml = _parse_testcase_lines(lines)
    if from_xml is not None:
        return from_xml

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    started_but_unfinished: set[str] = set()

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        summary = _CLASS_SUMMARY_RE.search(line)
        if summary:
            name = summary.group("cls")
            started_but_unfinished.discard(name)

            total = int(summary.group("total"))
            failures = int(summary.group("failures"))
            errors = int(summary.group("errors"))
            skipped = int(summary.group("skipped"))

            # Classify from the counts, not from the [INFO]/[ERROR] prefix.
            # Surefire's choice of prefix varies with the reporting configuration;
            # the numbers on the same line do not.
            if failures or errors:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif total > 0 and skipped == total:
                # Every method in the class was skipped (@Ignore, a failed
                # assumption). A class where only SOME methods skipped still has
                # passing ones and is reported as passed.
                if name not in failed_tests:
                    passed_tests.discard(name)
                    skipped_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            continue

        running = _RUNNING_RE.search(line)
        if running:
            started_but_unfinished.add(running.group("cls"))

    # Anything still here announced itself and never reported a result.
    for name in started_but_unfinished:
        if name not in passed_tests and name not in skipped_tests:
            failed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("commonmark", "commonmark-java")
class CommonmarkJava(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CommonmarkJavaImageDefault(self.pr, self._config)

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
        return parse_maven_surefire_log(test_log)
