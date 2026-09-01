import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class S3fsNioImageBase(Image):
    """Heavy, self-contained environment image (``base-pr-<N>``).

    Owns the runtime, the clone, the ``BASE_COMMIT`` pin and the git-history
    scrub. ``dockerfile()`` emits only ``WORKDIR /home/`` plus the clone line;
    ``DockerfileEnhancer`` then injects the syntax directive, build/proxy ARGs,
    ENV block, OCI labels and the CA-cert symlink farm directly after ``FROM``,
    and rewrites the clone into clone + checkout + history scrub + ``CMD``.

    No apt layer: ``maven:3.9-eclipse-temurin-11`` already ships git 2.43,
    Temurin JDK 11, Maven 3.9.16 and ``/etc/ssl/certs/ca-certificates.crt``, so
    every D10 dimension this stack needs is satisfied by the base image itself.
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

    def dependency(self) -> str:
        # pom.xml at e0daefdd pins maven-compiler-plugin source/target to 1.8 and
        # JUnit 5.7.0 with surefire 3.0.0-M5; the JDK 11 Temurin image builds and
        # runs it cleanly, and ships Maven so no separate `maven` package is needed.
        return "maven:3.9-eclipse-temurin-11"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Emit only WORKDIR + the clone. DockerfileEnhancer prepends the syntax
        # directive, ARGs, ENV, LABELs and the CA-cert farm after FROM, and
        # _standardize_repo_fetch rewrites the clone line below into
        # clone -> WORKDIR -> reset -> checkout ${BASE_COMMIT} -> history scrub
        # -> submodule scrub -> CMD. Nothing else belongs here: an apt layer or
        # a stray ENV between WORKDIR and the clone would break that shape.
        #
        # The clone URL is spelled out rather than written as "${REPO_URL}":
        # _standardize_repo_fetch's Pattern 2 carries a negative lookahead
        # (?!"\\$\\{REPO_URL\\}") and therefore skips a line that already uses the
        # ARG, which would leave the reset/checkout/CMD block un-injected.
        return f"""FROM {self.dependency()}

WORKDIR /home/

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


class S3fsNioImageDefault(Image):
    """Thin PR layer (``pr-<N>``) built on top of :class:`S3fsNioImageBase`.

    Stages only the two patches and the run scripts, then runs ``prepare.sh``
    once at build time. It deliberately does not clone, checkout, apt-install
    or scrub -- every one of those is owned by the base image.
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

    def dependency(self) -> Image | None:
        return S3fsNioImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Xmx4g
mvn -B clean test -Dmaven.test.skip=false -DfailIfNoTests=false || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Xmx4g
mvn -B clean test -Dmaven.test.skip=false -DfailIfNoTests=false
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Xmx4g
git apply --whitespace=nowarn /home/test.patch
mvn -B clean test -Dmaven.test.skip=false -DfailIfNoTests=false

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Xmx4g
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
mvn -B clean test -Dmaven.test.skip=false -DfailIfNoTests=false

""".format(pr=self.pr),
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


@Instance.register("carlspring", "s3fs-nio")
class S3fsNio(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return S3fsNioImageDefault(self.pr, self._config)

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

        # s3fs-nio Surefire 3.0.0-M5 output format: the class name is on the SAME
        # line as the counts, after " - in ", so no "Running" state is needed:
        #   [INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.032 s - in org.carlspring.cloud.storage.s3fs.fileSystemProvider.DeleteTest
        #   [ERROR] Tests run: 4, Failures: 0, Errors: 2, Skipped: 0, Time elapsed: 0.034 s <<< FAILURE! - in org.carlspring.cloud.storage.s3fs.fileSystemProvider.DeleteTest
        # Requiring the " - in <FQCN>" suffix means the "Results:" summary line
        # ("Tests run: 398, Failures: 0, Errors: 0, Skipped: 0") never becomes a
        # test name.
        result_pattern = re.compile(
            r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)"
            r"(?:, Time elapsed: [\d.,]+ s(?:ec)?)?"
            r"(?: <<< (?:FAILURE|ERROR)!)? - in ([\w.$]+)"
        )

        for line in test_log.splitlines():
            result_match = result_pattern.search(line)
            if not result_match:
                continue

            tests_run = int(result_match.group(1))
            failures = int(result_match.group(2))
            errors = int(result_match.group(3))
            skipped = int(result_match.group(4))
            test_name = result_match.group(5)

            if failures > 0 or errors > 0:
                failed_tests.add(test_name)
            elif tests_run > 0 and skipped == tests_run:
                skipped_tests.add(test_name)
            elif tests_run > 0:
                passed_tests.add(test_name)

        # A class re-run across stages must never land in two buckets; failure wins.
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
