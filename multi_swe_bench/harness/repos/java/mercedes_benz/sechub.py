"""Repo config for mercedes-benz/sechub (Java 11 / Gradle 7.6 / JUnit 5).

The graded unit is ``:sechub-pds:test``: every file the fix patch touches lives
in ``sechub-pds/src/main/java``, and the gold test patch adds
``PDSGetJobStreamServiceTest`` plus a mock wiring change in
``PDSExecutionCallableTest`` under ``sechub-pds/src/test/java``. The remaining
two test-patch files are integration-test fixtures for ``:sechub-integrationtest``,
which CI runs separately against a booted server and which carries no assertion
about this fix.

Both gold test files reference ``PDSGetJobStreamService``, a class the fix patch
creates, so in the test stage ``compileTestJava`` fails. javac is all-or-nothing
per source set: that one unresolvable symbol stops all ~329 tests in the module
from running, and the stage would otherwise report a bare 0/0/0 -- every test
NONE, every verdict left to be inferred from the baseline run instead of
observed. ``quarantine.py`` narrows the blast radius to the sources that
actually cannot compile:

* a test file that *exists* at the base revision (here ``PDSExecutionCallableTest``,
  which the test patch only rewires) is restored with ``git checkout HEAD --``,
  so its tests stay in the run and report what they report at baseline;
* a test file *added* by the test patch (here ``PDSGetJobStreamServiceTest``) is
  moved out of the source set, and its ``@Test`` methods are emitted as FAILED --
  a test that cannot compile is a failing test.

The module then compiles and the test stage reports 321 passed + 8 failed
instead of 0/0/0, which is the state the classifier reads directly: 321 p2p and
8 f2p, no reclassification from the baseline needed. The fix stage compiles
unmodified and runs all 329.

Only ``compileTestJava FAILED`` triggers any of this, and it never fires in the
run or fix stages, so the three stage scripts still execute a byte-identical
body. Synthesising a test id is refused wherever it could be wrong -- a
``@ParameterizedTest``/``@RepeatedTest`` id embeds an invocation display name
and a ``@Nested`` id embeds ``Outer$Inner``, neither reconstructible from source
-- and those tests fall back to the NONE -> PASS (n2p) path exactly as before.

Results are read from Gradle's JUnit XML rather than from console output:
``gradle/build-java.gradle`` configures ``test`` without ``testLogging``, so the
console prints no per-test line, and the ``gradle.buildFinished`` hook it
installs prints ``class::method`` only for *failures*. ``collect_results.py``
re-emits every ``<testcase>`` as
``<repo-relative source path>::<fqcn>#<method>``. The path head is required:
``Report._test_name_matches_files`` matches a test name against the patch file
list only when the name starts with a repo-root-relative path, and a bare JVM id
would make the matcher fail open and credit phantom n2p tests.

Two ``@ParameterizedTest`` methods in one class report identical JUnit XML
``name`` attributes (``[1] value=``), because the invocation display name does
not carry the method. ``junit_names.gradle`` prefixes ``{displayName}`` onto the
default parameterized name so the ids stay distinct; it is an ``--init-script``
rather than a repo edit, so it cannot dirty the tree the patches apply to.

``build-java.gradle`` also sets ``ignoreFailures = true`` on ``test`` but rethrows
from ``gradle.buildFinished`` when anything failed, so ``gradlew`` exits non-zero
on a red suite. The exit code is captured instead of aborting the stage, and the
runner-start guarantee is enforced separately by grepping for Gradle's own
``BUILD SUCCESSFUL``/``BUILD FAILED`` line.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Shared by run.sh / test-run.sh / fix-run.sh -- byte-identical in all three, so
# the only difference between stages is which patch was applied beforehand.
_TEST_BODY = """\
# prepare.sh already ran this task to warm the Gradle caches, so its XML is
# baked into the image. Deleting the outputs both discards that stale result and
# makes the task out-of-date, which is what forces Gradle to execute it again.
QUARANTINED=/tmp/quarantined.txt
QUARANTINE_DIR=/tmp/quarantine
: > "$QUARANTINED"
rm -rf "$QUARANTINE_DIR"

attempt=0
while :; do
    attempt=$((attempt + 1))
    rm -rf sechub-pds/build/test-results sechub-pds/build/reports/tests

    # Not aborted on failure: build-java.gradle rethrows from gradle.buildFinished
    # whenever a test failed, and the XML still has to be collected afterwards.
    set +e
    ./gradlew --no-daemon --console=plain --init-script /home/junit_names.gradle :sechub-pds:test 2>&1 | tee /tmp/gradle.out
    GRADLE_RC=$?
    set -e
    echo "NOTE: gradlew exited ${GRADLE_RC} (attempt ${attempt})"

    # javac is all-or-nothing per source set, so one unresolvable symbol in one
    # test file keeps every test in the module from running. That is the single
    # recoverable failure here, and only by taking the offending sources out of
    # the source set. Anything else is a real result and is collected as-is.
    if ! grep -q "compileTestJava FAILED" /tmp/gradle.out; then
        break
    fi
    if [ "${attempt}" -ge 3 ]; then
        echo "NOTE: giving up after ${attempt} compileTestJava failures"
        break
    fi

    BAD=$(python3 /home/quarantine.py detect /tmp/gradle.out)
    if [ -z "${BAD}" ]; then
        # Compilation broke somewhere quarantine.py will not touch (a main
        # source, or a path it cannot resolve). Nothing safe to remove.
        break
    fi
    for bad in ${BAD}; do
        if git cat-file -e "HEAD:${bad}" 2>/dev/null; then
            # Pre-existing file the test patch edited: restoring the base
            # revision keeps its tests in the run, reporting what they report at
            # baseline, instead of silently dropping them from the stage.
            git checkout HEAD -- "${bad}"
            echo "NOTE: reverted ${bad} to the base revision"
        else
            # Added by test.patch and unbuildable until fix.patch lands. Its
            # tests genuinely cannot run; quarantine.py names them FAILED below.
            mkdir -p "${QUARANTINE_DIR}/$(dirname "${bad}")"
            mv "${bad}" "${QUARANTINE_DIR}/${bad}"
            echo "${bad}" >> "${QUARANTINED}"
            echo "NOTE: quarantined uncompilable new test source ${bad}"
        fi
    done
done

python3 /home/quarantine.py emit "${QUARANTINED}" "${QUARANTINE_DIR}"
python3 /home/collect_results.py

# Runner-start guarantee: Gradle prints this line whether the build passed or
# failed, so its absence means gradlew never got as far as running anything and
# the stage must fail loudly instead of reporting a silent 0/0/0.
grep -qE "^BUILD (SUCCESSFUL|FAILED)" /tmp/gradle.out
"""


class SechubImageBase(Image):
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
        # gradle/build-java.gradle pins source/targetCompatibility to 11 and
        # .github/workflows/gradle.yml runs temurin 11. Both amd64 and arm64
        # variants of this tag are published.
        return "eclipse-temurin:11-jdk-jammy"

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

        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV CI=true
ENV GRADLE_USER_HOME=/root/.gradle

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git python3 unzip \\
    && rm -rf /var/lib/apt/lists/*

# GRADLE_USER_HOME properties outrank the project's gradle.properties, which
# caps the daemon at -Xmx2048m -- too small for a Spring Boot test run here.
RUN mkdir -p /root/.gradle \\
    && printf 'org.gradle.jvmargs=-Xmx4g\\n' > /root/.gradle/gradle.properties

{code}

{self.clear_env}

"""


class SechubImageDefault(Image):
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
        return SechubImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                "junit_names.gradle",
                """// Two @ParameterizedTest methods in one class otherwise emit the same JUnit
// XML name ("[1] value="), collapsing distinct tests into one id. Applied as an
// init script so the repo under test stays byte-identical to the patched tree.
allprojects {
    tasks.withType(Test).configureEach {
        systemProperty 'junit.jupiter.params.displayname.default', '{displayName} [{index}] {argumentsWithNames}'
    }
}
""",
            ),
            File(
                ".",
                "collect_results.py",
                '''#!/usr/bin/env python3
"""Re-emit Gradle's JUnit XML as one stable line per test case.

Name shape: ``<repo-relative source path>::<fqcn>#<method>``. The path head lets
Report's patch-file matcher resolve the name; the fqcn keeps it unique across
modules and nested classes.
"""

import glob
import os
import xml.etree.ElementTree as ET

REPO = "/home/{repo}"

found = 0
pattern = os.path.join(REPO, "*", "build", "test-results", "test", "*.xml")
for xml_path in sorted(glob.glob(pattern)):
    module = os.path.relpath(xml_path, REPO).split(os.sep)[0]
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        continue
    for case in root.iter("testcase"):
        classname = (case.get("classname") or "").strip()
        name = (case.get("name") or "").strip()
        if not classname or not name:
            continue
        # JUnit 5 display names are "method()"; drop the empty parens so the id
        # is identical no matter which engine reported the case.
        if name.endswith("()"):
            name = name[:-2]
        if case.find("skipped") is not None:
            status = "SKIPPED"
        elif case.find("failure") is not None or case.find("error") is not None:
            status = "FAILED"
        else:
            status = "PASSED"
        outer = classname.split("$", 1)[0].replace(".", "/")
        src = "{{0}}/src/test/java/{{1}}.java".format(module, outer)
        print("TEST_RESULT|{{0}}|{{1}}::{{2}}#{{3}}".format(status, src, classname, name))
        found += 1

print("TEST_RESULT_TOTAL|{{0}}".format(found))
'''.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "quarantine.py",
                '''#!/usr/bin/env python3
"""Rescue a Gradle test stage that died in ``compileTestJava``.

javac compiles a source set as a unit, so a single test file referencing a
symbol only ``fix.patch`` introduces stops every test in the module from
running. The stage would then report 0/0/0 -- indistinguishable from "the runner
never started", and far less informative than the truth, which is that all but a
handful of the module's tests are perfectly runnable.

``detect`` names the test sources javac actually complained about; the stage
script restores the ones that exist at the base revision and quarantines the
ones ``test.patch`` added. ``emit`` then reports the quarantined files' tests as
FAILED, which is what a test that cannot compile is.

An emitted id has to match the id the fix stage produces from JUnit XML byte for
byte, or it becomes a phantom test present in one stage only. So emission is
refused for anything whose id is not reconstructible from source alone:
``@ParameterizedTest``/``@RepeatedTest`` invocations carry a display name, and
``@Nested`` classes report as ``Outer$Inner``. Those keep the NONE -> PASS (n2p)
path they had before.
"""

import os
import re
import sys

REPO = "/home/{repo}"

# javac: ``/abs/path/Foo.java:24: error: cannot find symbol``.
_ERROR_LINE = re.compile(r"^(\\S+\\.java):[0-9]+: error:")
# ``@Test``, but not ``@TestFactory``/``@TestTemplate``: \\b cannot match before a
# word character, so only the bare annotation (or ``@Test(expected=...)``) hits.
_PLAIN_TEST = re.compile(r"^@Test\\b")
_METHOD = re.compile(r"(\\w+)\\s*\\(")
_PACKAGE = re.compile(r"^\\s*package\\s+([\\w.]+)\\s*;", re.MULTILINE)
# Ids for these embed an invocation display name or a nested-class separator,
# neither of which can be reconstructed from the source file alone.
_UNRECONSTRUCTABLE = ("@ParameterizedTest", "@RepeatedTest", "@TestTemplate",
                      "@TestFactory", "@Nested")


def detect(gradle_log):
    """Repo-relative paths of the *test* sources javac reported errors in."""
    found = []
    with open(gradle_log, errors="replace") as handle:
        for line in handle:
            match = _ERROR_LINE.match(line.strip())
            if not match:
                continue
            path = match.group(1)
            if os.path.isabs(path):
                if not path.startswith(REPO + os.sep):
                    continue
                path = os.path.relpath(path, REPO)
            # Only a test source can leave the build without changing what the
            # code under test does.
            if "/src/test/" not in path:
                continue
            if path not in found:
                found.append(path)
    return found


def _test_methods(text):
    """Names of the plain ``@Test`` methods declared in a Java source."""
    lines = text.splitlines()
    names = []
    for index, line in enumerate(lines):
        if not _PLAIN_TEST.match(line.strip()):
            continue
        # Walk past any further annotations/comments to the signature itself.
        for probe in lines[index + 1:]:
            stripped = probe.strip()
            if not stripped or stripped.startswith("@") or stripped.startswith("//"):
                continue
            match = _METHOD.search(stripped)
            if match and match.group(1) not in names:
                names.append(match.group(1))
            break
    return names


def emit(list_file, quarantine_root):
    if not os.path.exists(list_file):
        return
    with open(list_file) as handle:
        for raw in handle:
            rel = raw.strip()
            if not rel:
                continue
            source = os.path.join(quarantine_root, rel)
            if not os.path.exists(source):
                continue
            text = open(source, errors="replace").read()
            if any(marker in text for marker in _UNRECONSTRUCTABLE):
                print("NOTE: not naming tests in {{0}} (unreconstructable ids)".format(rel))
                continue
            package = _PACKAGE.search(text)
            cls = os.path.basename(rel)[:-len(".java")]
            fqcn = "{{0}}.{{1}}".format(package.group(1), cls) if package else cls
            for method in _test_methods(text):
                # Same id shape as collect_results.py, so the stages agree.
                print("TEST_RESULT|FAILED|{{0}}::{{1}}#{{2}}".format(rel, fqcn, method))


if __name__ == "__main__":
    if sys.argv[1:2] == ["detect"]:
        for path in detect(sys.argv[2]):
            print(path)
    elif sys.argv[1:2] == ["emit"]:
        emit(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("usage: quarantine.py detect <log> | emit <list> <dir>")
'''.format(repo=self.pr.repo),
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

# The root build calls Grgit.open() at configuration time, so the clone must
# keep its .git directory; nothing here removes it.
git config --global --add safe.directory /home/{pr.repo}
chmod +x gradlew

# Warms the Gradle distribution, the plugin portal resolutions and every
# compile dependency into the image. `|| true` because a cold-cache resolution
# failure or an arm64 hiccup must not abort the build -- the graded stages
# resolve again and surface any real breakage as test results.
./gradlew --no-daemon --console=plain --init-script /home/junit_names.gradle :sechub-pds:test || true

# Leave no test XML behind: a stage that fails to compile must report zero
# tests, not the results this warm-up produced.
rm -rf sechub-pds/build/test-results sechub-pds/build/reports/tests

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Emitted by collect_results.py, one line per <testcase> in Gradle's JUnit XML.
_RESULT_LINE = re.compile(r"^TEST_RESULT\|(PASSED|FAILED|SKIPPED)\|(.+)$")


def parse_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for line in clean.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if not match:
            continue
        status, name = match.group(1), match.group(2).strip()
        if not name:
            continue
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # TestResult.__post_init__ rejects overlapping sets. A case reported both
    # ways across retries is honestly a failure.
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


@Instance.register("mercedes-benz", "sechub")
class Sechub(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SechubImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_log(log)
