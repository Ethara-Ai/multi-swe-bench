import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Mirrors the package list baked into Image.dockerfile() (image.py) so the
# base image installs exactly the canonical toolchain.
_DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]


# ---------------------------------------------------------------------------
# sonar-findbugs: spotbugs/sonar-findbugs
# ---------------------------------------------------------------------------
# The SonarQube plugin that runs SpotBugs.  This is a *Maven* project (pom.xml,
# JUnit 4 + Mockito + fest-assert, surefire 3.0.0-M5) and is unrelated to
# `spotbugs/spotbugs` in this same directory, which is Gradle-based and
# registered separately.  Do not confuse the two.
#
# Image chain (the standard two-layer split -- see DOCKERFILE_QC_PROMPT.md):
#
#   ubuntu:22.04 -> base-pr-<N>  JDK 11 + Maven toolchain, git clone, checkout
#                                ${BASE_COMMIT}, history scrub.  The proxy/CA/
#                                LABEL header and the clone->checkout->harden
#                                tail are injected by DockerfileEnhancer, so
#                                this file must NOT emit them itself.
#                -> pr-<N>       inherits base-pr-<N>, COPYs the two patches +
#                                the run-scripts + surefire_report.py, and runs
#                                prepare.sh once.  It does not clone or scrub.
#
# THE GRADED CONFIGURATION IS SONAR 9 -- THIS IS THE WHOLE POINT.
# ---------------------------------------------------------------------------
# PR 369 resolves issue #368, "Replaced references to API removed in Sonar 9".
# pom.xml defaults to `sonar.version` 7.9, where the removed APIs still exist,
# so under the default properties the base commit compiles and all 63 tests
# pass -- the bug is invisible and the instance grades as 63 p2p / 0 f2p /
# 0 n2p, i.e. it would be discarded.
#
# The bug only reproduces under the Sonar 9 coordinates that fix.patch itself
# adds to the CI matrix in `.github/workflows/build.yml`:
#
#     - SONAR_VERSION: 9.0.0.45539
#       SONAR_JAVA_VERSION: 7.1.0.26670
#
# and which the CI passes as `-Dsonar.version=... -Dsonar-java.version=...`.
# Under those properties, verified end-to-end:
#
#     run   main sources fail to compile (removed Sonar APIs)  -> 0 tests
#     test  same failure; test.patch alone cannot help          -> 0 tests
#     fix   compiles and passes                                 -> 63 tests
#
# All 63 therefore land on the classifier's (run=NONE, test=NONE, fix=PASS)
# branch, where `_touched_by_test_patch` splits genuine n2p from phantoms.
#
# fix.patch does NOT touch pom.xml, so both states resolve the same dependency
# set and prepare.sh only has to warm ~/.m2 once (it warms both states anyway,
# cheaply, so the graded runs never need the network).
# ---------------------------------------------------------------------------

# The Sonar coordinates fix.patch adds to the CI matrix. Changing these changes
# what the instance measures -- see the block above before touching them.
_SONAR_PROPS = "-Dsonar.version=9.0.0.45539 -Dsonar-java.version=7.1.0.26670"

# `.github/workflows/build.yml` pins `java-version: 11` for every matrix leg,
# and pom.xml sets `jdk.min.version` 1.8; JDK 11 satisfies both.
_JDK_PACKAGE = "openjdk-11-jdk"


# ---------------------------------------------------------------------------
# Base Image
# ---------------------------------------------------------------------------


class SonarFindbugsImageBase(Image):
    """Per-PR base image - JDK 11 + Maven, repo cloned and pinned to
    ``BASE_COMMIT``."""

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

    def extra_packages(self) -> list[str]:
        # Java needs BOTH the runtime and the build tool from apt -- neither is
        # in an ubuntu base (QC appendix, Java items (1) and (2)).
        return [_JDK_PACKAGE, "maven"]

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # NOTE ON WHAT THIS FILE DELIBERATELY OMITS.
        # DockerfileEnhancer.enhance() (image.py) post-processes this string
        # because dependency() returns a str.  It supplies, and this template
        # must therefore NOT repeat:
        #   * the `# syntax=docker/dockerfile:1.6` directive
        #   * ARG TARGETARCH / REPO_URL / BASE_COMMIT + the proxy ARGs
        #   * the ENV block (DEBIAN_FRONTEND, LANG, TZ, proxy passthrough, TLS)
        #   * the OCI LABEL block and the CA-cert symlink farm
        # It also rewrites the bare `RUN git clone ... /home/<repo>` line below
        # into the canonical tail: clone "${REPO_URL}" -> WORKDIR /home/<repo>
        # -> git reset --hard -> git checkout ${BASE_COMMIT} -> history-scrub +
        # integrity asserts -> CMD ["/bin/bash"].  Nothing may follow that line.
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_DEFAULT_PACKAGES + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        return f"""\
FROM {base_img}

{self.global_env}

WORKDIR /home/

{apt_command}

{self.clear_env}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


# ---------------------------------------------------------------------------
# Instance Image
# ---------------------------------------------------------------------------


class SonarFindbugsImageDefault(Image):
    """Per-PR instance image.  Stages the patches, the run-scripts and the
    Surefire report parser, and warms the Maven repository."""

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
        return SonarFindbugsImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _apply_block(self, patches: str) -> str:
        """git-apply the given patches, failing loudly rather than silently
        grading an unpatched tree."""
        if not patches:
            return ""
        return """if ! git apply --whitespace=nowarn {patches}; then
    if ! git apply --whitespace=nowarn --3way {patches}; then
        echo "Error: failed to apply {patches}" >&2
        exit 1
    fi
fi
""".format(patches=patches)

    def _make_run_script(self, patches: str) -> str:
        """Generate a graded run script.

        `set -eo pipefail` with NO `|| true` on the test command, per the repo
        config QC standard: a runner that fails to *start* must surface rather
        than be swallowed into an empty 0/0/0 TestResult.

        The EXIT trap is what makes that safe here. Under the Sonar 9 properties
        the ``run`` and ``test`` stages fail to compile ON PURPOSE -- that is the
        signal this instance measures -- so `set -e` would abort before the
        report step and, if a future PR failed only *partway* through the suite,
        would discard the Surefire XML that had already been written. The trap
        guarantees the report always runs. It masks nothing: the script still
        exits with Maven's own status.

        ``rm -rf target/surefire-reports`` is load-bearing. Surefire does not
        clear stale XML, and prepare.sh leaves a populated reports directory
        behind from cache warming -- without this line a stage whose build
        FAILED would be graded against the previous stage's reports and every
        test would look like it passed.
        """
        return """#!/bin/bash
set -eo pipefail
export CI=true
export MAVEN_OPTS="-Xmx4g"

cd /home/{repo}
{apply_block}
# Never grade stale Surefire XML -- see the docstring in _make_run_script.
rm -rf target/surefire-reports

# Emit one stable `[TEST] <path>::<method> <STATUS>` line per JUnit test case,
# even when `mvn` exits non-zero under `set -e`.  Does not mask the failure.
trap 'python3 /home/surefire_report.py . 2>&1' EXIT

mvn -B {sonar} test 2>&1
""".format(
            repo=self.pr.repo,
            apply_block=self._apply_block(patches),
            sonar=_SONAR_PROPS,
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "surefire_report.py",
                '''#!/usr/bin/env python3
"""Emit one stable line per JUnit test case from Maven Surefire XML reports.

Maven has no machine-readable console reporter, so the graded signal is taken
from `target/surefire-reports/TEST-*.xml` instead of stdout. Each test case
becomes exactly one line:

    [TEST] src/test/java/<pkg>/<Class>.java::<methodName> PASS|FAIL|SKIP

WHY THE SOURCE PATH AND NOT THE FULLY-QUALIFIED CLASS NAME.
Surefire reports `classname` as an FQCN, but the harness classifier matches a
test to the patch that authored it with `_test_name_matches_files`
(harness/report.py), which compares the part BEFORE `::` against the patch's
file paths. An FQCN can never match a path, so `_file_matcher_can_hit` returns
False, `_test_patch_matcher_ok` goes False, and `_touched_by_test_patch`
silently falls back to `return True` -- crediting EVERY test as n2p, including
tests the test patch never touched. Emitting the real repo-relative path makes
that guard work as intended, so only tests in files the test patch actually
modified get credited.

The class qualifier is also what makes the identifier unique: JUnit method
names such as `test` recur across many classes in this repo.

A `<failure>` or `<error>` child means FAIL, `<skipped>` means SKIP, and a bare
`<testcase>` means PASS.
"""
import os
import sys
import xml.etree.ElementTree as ET

root_dir = sys.argv[1] if len(sys.argv) > 1 else "."

# Maven standard layout. Checked against the filesystem before use so a
# non-standard or generated class degrades to its FQCN rather than inventing a
# path that no patch could ever match.
_SRC_ROOTS = ("src/test/java", "src/test/kotlin", "src/main/java")


def source_path(classname):
    if not classname:
        return ""
    # Strip inner/nested class suffixes: "FooTest$Inner" -> "FooTest".
    outer = classname.split("$", 1)[0]
    rel = outer.replace(".", "/")
    for src_root in _SRC_ROOTS:
        for ext in (".java", ".kt"):
            candidate = os.path.join(src_root, rel + ext)
            if os.path.isfile(os.path.join(root_dir, candidate)):
                return candidate
    return ""


for dirpath, _dirnames, filenames in os.walk(root_dir):
    if os.path.basename(dirpath) != "surefire-reports":
        continue
    for fn in sorted(filenames):
        if not (fn.startswith("TEST-") and fn.endswith(".xml")):
            continue
        try:
            tree = ET.parse(os.path.join(dirpath, fn))
        except ET.ParseError:
            # A run killed mid-write leaves truncated XML; skip it rather than
            # aborting and losing every other report.
            continue
        for case in tree.getroot().iter("testcase"):
            cls = case.get("classname") or ""
            name = case.get("name") or ""
            if not name:
                continue
            status = "PASS"
            for child in case:
                tag = child.tag.split("}")[-1]
                if tag in ("failure", "error"):
                    status = "FAIL"
                    break
                if tag == "skipped":
                    status = "SKIP"
                    break
            head = source_path(cls) or cls
            ident = "{}::{}".format(head, name) if head else name
            print("[TEST] {} {}".format(ident, status))
''',
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
export MAVEN_OPTS="-Xmx4g"

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

# Warm ~/.m2 for the graded Sonar 9 coordinates.  Build time is the only point
# where the network is guaranteed.  `|| true` is required: at the base commit
# these properties do not compile -- that IS the bug -- but the failing build
# still resolves every dependency and plugin we need.
mvn -B {sonar} test || true

# Warm the fixed state too, so the fix run never reaches the registry either.
# fix.patch leaves pom.xml alone, so this normally adds nothing; it is cheap
# insurance against a future PR in this repo that does change dependencies.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn -B {sonar} test || true

# Restore the pristine base commit. BOTH steps are required:
#   git checkout -- .  reverts the tracked files the patches modified;
#   git clean -fdx     removes what checkout cannot -- the file test.patch ADDS
#                      (src/test/.../configuration/SimpleConfiguration.java) and
#                      the whole gitignored `target/` tree.  Leaving a populated
#                      surefire-reports behind would make the `run` stage grade
#                      against this cache-warming run's results.
git checkout -- .
git clean -fdx

bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha, sonar=_SONAR_PROPS),
            ),
            # run.sh - baseline: no patches
            File(".", "run.sh", self._make_run_script("")),
            # test-run.sh - test.patch only
            File(".", "test-run.sh", self._make_run_script("/home/test.patch")),
            # fix-run.sh - test.patch + fix.patch (test patch first)
            File(
                ".",
                "fix-run.sh",
                self._make_run_script("/home/test.patch /home/fix.patch"),
            ),
        ]

    def dockerfile(self) -> str:
        # Intentionally tiny.  Everything heavy -- toolchain, clone, the
        # BASE_COMMIT pin, the proxy/CA trust and the history scrub -- is
        # already earned by base-pr-<N>.  This layer only stages the patches
        # and run-scripts and runs prepare.sh once, per the P-series contract.
        # It must NOT clone, apt-install, re-scrub, or re-declare the ARGs:
        # dependency() returns an Image, so DockerfileEnhancer leaves this
        # string untouched and whatever is written here is what gets built.
        dep = self.dependency()
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""\
FROM {dep.image_full_name()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh
"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("spotbugs", "sonar-findbugs")
class SonarFindbugs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SonarFindbugsImageDefault(self.pr, self._config)

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
        """Parse the `[TEST] <class>::<method> <STATUS>` lines emitted by
        ``surefire_report.py`` (staged into the image by ``files()``).

        Maven's console output is not machine-readable per test -- Surefire
        prints only per-class roll-ups plus free-text failure blocks -- so the
        graded signal comes from the Surefire XML instead, normalised by that
        helper into one line per test case.

        A stage whose Maven build fails to compile emits no lines at all, which
        the harness scores as ``NONE`` for every test. That is the expected
        shape of the ``run`` and ``test`` stages here; see the module docstring.
        """
        # Strip ANSI first. `mvn -B` disables colour and surefire_report.py
        # prints plain text, so nothing is expected here -- but a coloured
        # wrapper upstream must not be able to break the match.
        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        re_test = re.compile(r"^\[TEST\]\s+(\S+)\s+(PASS|FAIL|SKIP)\s*$")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in test_log.split("\n"):
            m = re_test.match(ansi.sub("", line).strip())
            if not m:
                continue
            name, status = m.group(1), m.group(2)
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # A name seen failing anywhere is a failure; likewise a failure
        # outranks a skip. Keeps the TestResult set invariants satisfied.
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
