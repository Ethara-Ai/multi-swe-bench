"""A248/DazzleConf - multi-module Maven library, JDK 17 build / Java 11 target.

Every value below was observed by running the real toolchain in Docker at base
commit 579d4032, not inferred from the manifests:

  pom.xml   maven-enforcer-plugin:3.0.0-M3
            requireJavaVersion 15.0.1   -> build-time JDK >= 15, NOT 8 or 11
            requireNoRepositories       -> the POM may not declare <repository>
            junit.version 5.7.0         -> JUnit 5 (Jupiter)
            maven-surefire-plugin 3.0.0-M5
            maven-compiler-plugin 3.8.1
              default-compile / default-testCompile  source+target 11
              base-compile                           excludes module-info.java at 1.8
            modules: core, gson, snakeyaml
  apt       maven 3.6.3, openjdk 17.0.19 on ubuntu:22.04

Three things are worth knowing before changing anything here.

1. The enforcer, not the bytecode target, sets the JDK. `<source>/<target>` are
   11 and `module-info.java` only needs 9+, so JDK 11 looks sufficient from the
   manifests. It is not: requireJavaVersion 15.0.1 aborts the reactor in the
   parent POM's `validate` phase, before a single class compiles. openjdk-11-jdk
   and openjdk-8-jdk - the two most common picks in this tree - both fail there.
   openjdk-17-jdk was run end to end and satisfies every plugin in the build.

2. No Maven mirror is injected. `requireNoRepositories` forbids adding one to the
   POM, so a mirror would have to live in ~/.m2/settings.xml. None is needed:
   Maven Central resolved the entire dependency tree - including the fix patch's
   com.typesafe:config:1.4.1 - without a single rate-limit error, unlike the
   Android-era projects in this tree that have to route around HTTP 429.

3. THIS IS AN n2p INSTANCE, NOT AN f2p ONE. PR #6 adds a whole new Maven module.
   fix_patch creates `hocon/pom.xml` AND adds `<module>hocon</module>` to the root
   POM; test_patch only writes files under `hocon/src/test/java/`. With the test
   patch alone that directory is not part of the reactor, so Maven never compiles
   it and never runs it. Measured, at this base commit:

     run   18 test classes / 45 tests   BUILD SUCCESS
     test  18 test classes / 45 tests   BUILD SUCCESS   <- class set IDENTICAL to run
     fix   19 test classes / 47 tests   BUILD SUCCESS   <- + HoconConfigurationFactoryTest

   The test stage matching the run stage exactly is the CORRECT outcome here, not
   a broken test-patch application (`git apply` was confirmed to succeed at both
   stages). The new class is NONE -> NONE -> PASS, which Report classifies as n2p
   because `HoconConfigurationFactoryTest` is authored in test_patch's added
   lines. Do not "fix" this by folding the module wiring into test_patch: that
   would move the reactor change out of the graded fix and hand the solution to
   the agent.
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
# inventing transitions that never happened and hiding the real ones. Surefire
# failures are expected at the test stage on f2p-shaped PRs - the stage must
# record them and keep going, not stop the reactor. Grading reads parse_log, never
# the exit code, so suppressing the non-zero exit costs nothing.
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
#              correctly - but only via the alternatives link. Setting it makes
#              the graded stages independent of alternatives state entirely.
#   PATH       Puts JDK 17's bin ahead of anything the base image may add later.
#   LC_ALL     The enhancer sets LANG but not LC_ALL, which leaves LC_COLLATE at
#              the C default; Surefire orders report output by locale.
#   MAVEN_OPTS Surefire forks a JVM per module and an AWT-touching test classpath
#              probes for a display during static init.
SHELL_ENV = """\
export JAVA_HOME=/usr/lib/jvm/java-17
export PATH="$JAVA_HOME/bin:$PATH"
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Djava.awt.headless=true"""


# Emitted after every graded Maven run and parsed by parse_log.
#
# Surefire's console output names an individual test METHOD only when it fails;
# passing methods appear nowhere except the XML reports under
# target/surefire-reports/. Grading purely off the console therefore collapses to
# one entry per test CLASS, which is coarser than every other language in this
# tree (pytest instances report `path::test_name`) and hides a real signal: a
# class where one method flips FAIL->PASS while another flips PASS->FAIL looks
# unchanged at class granularity.
#
# This reads whatever XML is on disk, so it is only correct if the tree holds
# nothing but the stage that just ran. Do not assume `mvn clean test` guarantees
# that: `clean` only touches modules in the reactor, and `hocon` is not in the
# reactor at the base commit. An orphaned hocon/target/surefire-reports/ once made
# all three stages report an identical 47 methods and emptied every bucket. The
# graded scripts delete every surefire-reports directory before Maven runs, and
# prepare.sh finishes with `git clean -fdx`; both are load-bearing.
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
# about which module a class came from in a multi-module reactor.
def source_path(root, xml_path, classname):
    module_dir = os.path.dirname(os.path.dirname(os.path.dirname(xml_path)))
    # Outer$Inner and Outer$1 both live in Outer.java.
    top_level = classname.split("$", 1)[0]
    relative = top_level.replace(".", os.sep) + ".java"

    candidate = os.path.join(module_dir, "src", "test", "java", relative)
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, root)

    # Non-standard source root (src/test/kotlin, a build-helper extra root, a
    # generated suite). Fall back to locating the file by name in the module.
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
            # `path::name` is how every other language in this dataset identifies
            # a test. The FQCN form is a last resort for a class whose source
            # cannot be located (e.g. a suite inherited from a dependency jar);
            # it keeps the test reportable instead of dropping it.
            if path:
                test_id = "{0}::{1}".format(path, name)
            else:
                test_id = "{0}#{1}".format(classname, name)
            print("SUREFIRE_TESTCASE {0} {1}".format(status_of(testcase), test_id))


if __name__ == "__main__":
    main()
"""


class DazzleConfImageBase(Image):
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
        # Dockerfile contains exactly one - the block DockerfileEnhancer injects.
        # dependency() returns a plain string, so the enhancer always rewrites
        # this file (build_dataset.py is its only writer and calls it
        # unconditionally), and that block already supplies DEBIAN_FRONTEND, LANG
        # and TZ; declaring them again produced a literal duplicate.
        #
        # The JDK variables the enhancer does NOT supply moved to SHELL_ENV, which
        # every Maven-invoking script exports. Nothing is lost: this build runs
        # only apt and git, neither of which reads them. What the image itself
        # must still guarantee - that `java` means 17 for anyone who opens a shell
        # in the container - is handled by pinning alternatives below, which is
        # strictly stronger than an ENV would have been.
        #
        # The rationale for the two RUN blocks below is kept HERE, in the module,
        # rather than as `#` lines inside the rendered Dockerfile - the generated
        # artifact ships to the client and is meant to read clean:
        #
        # apt: openjdk-17-jdk, not -11 or -8 - the parent POM's enforcer requires
        #   JDK >= 15.0.1 (see the module docstring). `--no-install-recommends` is
        #   safe despite the usual Java warning: the package that must survive is
        #   ca-certificates-java (it seeds the JKS truststore, without which Maven
        #   cannot complete a TLS handshake with Central), and on Ubuntu 22.04 that
        #   is a *Depends* of openjdk-17-jre-headless, not a Recommends - confirmed
        #   with `apt-cache depends` and by building with the flag and fetching a
        #   real artifact from Central through the resulting image.
        #
        # JDK pin: the `maven` package DEPENDS on default-jre-headless, so it drags
        #   openjdk-11 in alongside 17 and points /usr/lib/jvm/default-java at it.
        #   `java` still resolves to 17, but only because Debian ranks its
        #   alternatives priority higher and leaves the link in `auto` mode -
        #   nothing declares the choice. `--set` switches those links to manual
        #   mode, so the enforcer cannot start failing on a rebuild that reorders
        #   priorities or adds another JDK. Paths are resolved at build time rather
        #   than written literally because the pipeline builds linux/amd64 AND
        #   linux/arm64 from this one Dockerfile and Ubuntu's JVM directory carries
        #   the architecture in its name (java-17-openjdk-amd64 vs -arm64); a
        #   hardcoded path would silently break one leg. /usr/lib/jvm/openjdk-17
        #   exists but is an empty packaging stub, so the stable arch-independent
        #   symlink is made here instead, and is what SHELL_ENV points JAVA_HOME at.
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git python3 ca-certificates openjdk-17-jdk maven \\
    && rm -rf /var/lib/apt/lists/*

RUN JDK="/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)" \\
    && ln -sfn "$JDK" /usr/lib/jvm/java-17 \\
    && test -x /usr/lib/jvm/java-17/bin/java \\
    && for tool in java javac jar javadoc; do \\
           update-alternatives --set "$tool" "$JDK/bin/$tool"; \\
       done \\
    && test "$(readlink -f "$(command -v java)")" = "$JDK/bin/java"

{code}

{self.clear_env}

"""


class DazzleConfImageDefault(Image):
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
        return DazzleConfImageBase(self.pr, self._config)

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
# `timeout 1800` is NOT belt-and-braces on top of `|| true`. `|| true` handles a
# command that FAILS; a command that HANGS never returns, so it never reaches
# `||` at all - and Docker has no per-step timeout, so nothing else would break
# the deadlock. Maven blocking on a half-dead Central connection is exactly that
# shape.
#
# `|| true` still matters on its own: a build failure at the base commit is a
# legitimate state for some PRs and must not fail the image build. The verdict is
# recorded so a hollow image is DETECTABLE afterwards - without the marker, a
# warm-up that timed out on one architecture would look identical from the
# manifest to one that succeeded on the other.
# Inspect with: docker run <image> cat /home/.warm_status
warm() {{
  if timeout 1800 {mvn} > /tmp/warm.log 2>&1; then
    echo "warm-up $1: OK" >> /home/.warm_status
  else
    echo "warm-up $1: INCOMPLETE (exit $?)" >> /home/.warm_status
    tail -20 /tmp/warm.log || true
  fi
}}

# Pass 1 - the modules present at the base commit (core, gson, snakeyaml).
warm base

# Pass 2 - with both patches applied, so the dependencies the FIX introduces are
# cached too. PR #6 adds a new `hocon` module pulling com.typesafe:config:1.4.1,
# which pass 1 can never see because that module does not exist at the base
# commit. Without this the fix stage is the only stage that needs the network.
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
warm fix

cat /home/.warm_status

# Back to pristine so the graded stages apply their own patches cleanly.
#
# `-x` is REQUIRED, not tidiness. Without it git clean skips ignored files, and
# `target/` is ignored - so `git clean -fd` deleted hocon/src but left
# hocon/target/surefire-reports/ behind, baking the FIX stage's XML for
# HoconConfigurationFactoryTest into the image. Every later stage's report emitter
# then read that orphan, all three stages reported the same 47 methods, and every
# f2p/n2p/p2p bucket came out EMPTY - a silently worthless instance that still
# reported `valid: true`. `hocon` is not in the reactor at the base commit, so
# `mvn clean` can never remove it either.
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
# Defence in depth against the orphan-report failure above: `mvn clean` only
# cleans modules in the reactor, so a report directory belonging to a module that
# is not yet wired into the root POM would survive into this stage and be counted
# as though this stage had produced it. Removing them first means the emitter can
# only ever see what this stage just ran.
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
# Defence in depth against the orphan-report failure above: `mvn clean` only
# cleans modules in the reactor, so a report directory belonging to a module that
# is not yet wired into the root POM would survive into this stage and be counted
# as though this stage had produced it. Removing them first means the emitter can
# only ever see what this stage just ran.
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
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
# Defence in depth against the orphan-report failure above: `mvn clean` only
# cleans modules in the reactor, so a report directory belonging to a module that
# is not yet wired into the root POM would survive into this stage and be counted
# as though this stage had produced it. Removing them first means the emitter can
# only ever see what this stage just ran.
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

# Surefire's per-class summary, captured verbatim from the container:
#
#   [INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.036 s - in space.arim.dazzleconf.ext.snakeyaml.SnakeYamlConfigurationFactoryTest
#
# The trailing ` - in <FQCN>` is required by the pattern on purpose. Surefire also
# prints a per-module recap with the identical prefix and NO class name:
#
#   [INFO] Tests run: 21, Failures: 0, Errors: 0, Skipped: 0
#
# Matching that would add three phantom "tests" per run named after nothing, and
# their counts would shift whenever a real test changed - manufacturing f2p/p2p
# transitions out of a summary line.
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
#   SUREFIRE_TESTCASE PASS space.arim.dazzleconf.BadSubSectionTest#throwIllegalArgumentException
#
# The id is `<repo-relative source path>::<method>`, the same shape every other
# language in this dataset uses (pytest: `fiasco/tests/test_ion.py::test_x`).
# Surefire only knows the fully-qualified class name, so the emitter resolves it
# back to a real file on disk.
#
# `::` also gives Report TWO independent ways to attribute an n2p test, where the
# bare FQCN gave one: _candidate_identifiers splits on `::` and recovers the
# method name to look for in test_patch's added lines, AND _test_name_matches_files
# compares the path half against test_patch_files - which for this PR holds
# hocon/src/test/java/.../HoconConfigurationFactoryTest.java verbatim.
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
       normal path and matches the per-test granularity the rest of the dataset
       uses (`path::test_name` for pytest instances).

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
                # Every method in the class was skipped (@Disabled, a failed
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


@Instance.register("A248", "DazzleConf")
class DazzleConf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DazzleConfImageDefault(self.pr, self._config)

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
