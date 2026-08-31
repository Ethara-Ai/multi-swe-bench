import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Identical in all three graded stages - only patch application differs between them.
#
# This is the command tlaplus' own CI uses, minus `info`: that target depends on
# git-revision, which runs jgit over history the harness deliberately scrubs.
#
# -Dtest.halt=false is REQUIRED. At the test stage the new FcnLambdaValue tests are
# SUPPOSED to fail; with haltonfailure the junit task would abort the run and the stage
# would report zero tests - vacuously satisfying report.py's "fix something" check and
# producing a false-positive valid instance. A command-line -D wins over the
# <condition property="test.halt"> in customBuild.xml.
#
# `compile` itself depends on clean+generate, so each stage regenerates the SANY parser
# with javacc and rebuilds from source. That is why no separate clean step is needed.
TEST_CMD = (
    "ant -f tlatools/org.lamport.tlatools/customBuild.xml "
    "-Dtest.halt=false compile compile-test test"
)


class TlaplusImageBase(Image):
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
        # JDK 17 matches .github/workflows/main.yml ("Set up JDK 17"). The build targets
        # Java 11 via java.release=11 in customBuild.xml, which JDK 17 does natively - so
        # 17 is the JDK to run Ant with, not 11. -jammy pins the distro against tag drift.
        return "eclipse-temurin:17-jdk-jammy"

    def image_tag(self) -> str:
        # Per-PR base image. A shared "base" tag would be pinned to whichever
        # BASE_COMMIT built it first, and the history scrub then deletes every
        # other commit - so a second PR could not check its own base out.
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

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # DEBIAN_FRONTEND and LANG are deliberately NOT set here: DockerfileEnhancer
        # already injects both (with TZ and the proxy/CA wiring) in the block it places
        # right after FROM. Re-declaring them only produces duplicate ENV lines.
        #
        # ant-optional supplies the <junit> task customBuild.xml's test target uses; plain
        # `ant` does not carry it. python3 backs print_test_detail.sh, which reads Ant's
        # JUnit XML reports - this image has no interpreter of its own, so omitting it makes
        # every stage report zero tests while the suite itself runs perfectly. Every other
        # dependency (junit, hamcrest, cglib, gson, jline, javacc, jacoco - 30 jars) is
        # vendored in the repo's lib/ directory, so the test run needs no package repository.
        return f"""FROM {image_name}

{self.global_env}

ENV LC_ALL=C.UTF-8
ENV ANT_OPTS="-Xmx4g -Dfile.encoding=UTF-8"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates ant ant-optional python3 \\
    && rm -rf /var/lib/apt/lists/*

{code}

{copy_commands}

{self.clear_env}

"""


class TlaplusImageDefault(Image):
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
        return TlaplusImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "reset_reports.sh",
                # customBuild.xml's `clean` target (which `compile` depends on) wipes
                # class/ and test-class/ but NOT target/surefire-reports/, so the reports
                # would survive into the next stage and be read as its results. Removing
                # them here is what keeps each stage's results its own.
                """#!/bin/bash
rm -rf tlatools/org.lamport.tlatools/target/surefire-reports
""",
            ),
            File(
                ".",
                "print_test_detail.sh",
                # Ant's junit printsummary is per test CLASS, which is too coarse: a test
                # patch that adds methods to an existing class would collapse into one
                # entry and the added methods would be invisible. The xml formatter writes
                # one <testcase> per method with classname and name, which is the
                # granularity the classifier needs.
                """#!/bin/bash
echo "===== BEGIN JUNIT DETAIL ====="
python3 - <<'PYEOF'
import glob
import xml.etree.ElementTree as ET

paths = sorted(glob.glob("tlatools/org.lamport.tlatools/target/surefire-reports/TEST-*.xml"))
if not paths:
    print("NO_JUNIT_REPORTS")
for path in paths:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        continue
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        classname = case.get("classname") or ""
        ident = classname + "." + name if classname else name
        if case.find("failure") is not None or case.find("error") is not None:
            status = "FAILED"
        elif case.find("skipped") is not None:
            status = "SKIPPED"
        else:
            status = "PASSED"
        print("TESTCASE " + ident + " " + status)
PYEOF
echo "===== END JUNIT DETAIL ====="
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

# Hard gate, deliberately NOT tolerant. compile-test uses <compilerarg value="-Werror"/>
# and javac compiles the whole 717-file test tree in one pass, so a single warning takes
# the entire tree down and the stage would report zero tests. If the base tree cannot
# build, that must fail HERE rather than three stages later behind an empty report.
# This also runs javacc to generate the SANY parser, proving the vendored toolchain works.
ant -f tlatools/org.lamport.tlatools/customBuild.xml compile compile-test
echo "DEPS_OK"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/reset_reports.sh
set +e
{test_cmd}
ANT_RC=$?
set -e
echo "ANT_EXIT_CODE=$ANT_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/reset_reports.sh
set +e
{test_cmd}
ANT_RC=$?
set -e
echo "ANT_EXIT_CODE=$ANT_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/reset_reports.sh
set +e
{test_cmd}
ANT_RC=$?
set -e
echo "ANT_EXIT_CODE=$ANT_RC"
bash /home/print_test_detail.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
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


@Instance.register("tlaplus", "tlaplus")
class Tlaplus(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TlaplusImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text):
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?9;]*[mK]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # Only the lines emitted by print_test_detail.sh are authoritative. Ant's own
        # "Tests run: 12, Failures: 0" summary lines are ignored on purpose: they are
        # per-class totals with no method names, so treating them as results would
        # collapse every method in a class into one entry.
        case_re = re.compile(r"^TESTCASE (\S+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            if line.startswith("===== BEGIN JUNIT DETAIL ====="):
                in_detail = True
                continue
            if line.startswith("===== END JUNIT DETAIL ====="):
                in_detail = False
                continue
            if not in_detail:
                continue

            m = case_re.match(line)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
