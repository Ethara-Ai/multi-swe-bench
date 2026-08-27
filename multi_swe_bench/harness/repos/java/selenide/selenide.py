import base64
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GRADLE_CMD = "./gradlew test --no-daemon --console=plain"

_GRADLE_INIT_GRADLE = """\
gradle.beforeProject { project ->
    project.repositories.mavenCentral()
    project.afterEvaluate {
        def dead = project.repositories.findAll { repo ->
            repo instanceof org.gradle.api.artifacts.repositories.MavenArtifactRepository &&
                repo.url.toString().contains('jcenter.bintray.com')
        }
        dead.each { project.repositories.remove(it) }
    }
}

gradle.allprojects { project ->
    project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { task ->
        task.outputs.upToDateWhen { false }
        task.testLogging { logging ->
            logging.events 'passed', 'failed', 'skipped'
            logging.showStandardStreams = false
            logging.exceptionFormat 'short'
        }
    }
}
"""

_GRADLE_INIT_B64 = base64.b64encode(_GRADLE_INIT_GRADLE.encode()).decode()

_TEST_BODY = """
set +e
{gradle_cmd} > /tmp/gradle.out 2>&1
GRADLE_RC=$?
set -e

cat /tmp/gradle.out

if [ "$GRADLE_RC" -ne 0 ]; then
    echo "NOTE: gradle exited $GRADLE_RC; see the task results above"
fi

grep -q "^> Task :" /tmp/gradle.out
"""


class SelenideImageBase(Image):
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
        return "eclipse-temurin:8-jdk-jammy"

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

WORKDIR /home/

RUN printf 'Acquire::Retries "3";\\nAcquire::http::Timeout "20";\\nAcquire::https::Timeout "20";\\n' \\
        > /etc/apt/apt.conf.d/99-timeouts && \\
    sed -i '/^deb/{{ / main/!d }}' /etc/apt/sources.list && \\
    sed -i 's/ restricted//g; s/ universe//g; s/ multiverse//g' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /root/.gradle && \\
    echo "org.gradle.jvmargs=-Xmx2g -Dfile.encoding=UTF-8" > /root/.gradle/gradle.properties && \\
    echo "org.gradle.daemon=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.parallel=false" >> /root/.gradle/gradle.properties && \\
    echo "org.gradle.configureondemand=false" >> /root/.gradle/gradle.properties

RUN echo "{_GRADLE_INIT_B64}" | base64 -d > /root/.gradle/init.gradle

{code}

{self.clear_env}

"""


class SelenideImageDefault(Image):
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
        return SelenideImageBase(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew

{gradle_cmd} > /tmp/warmup.log 2>&1 || true

if ! grep -q "^> Task :" /tmp/warmup.log; then
    echo "FATAL: gradle never reached task execution during the warm-up" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

if ! grep -qE "^> Task :test( |$)" /tmp/warmup.log; then
    echo "FATAL: :test never executed during the warm-up" >&2
    grep -E "^> Task :.* FAILED$" /tmp/warmup.log >&2 || true
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

if grep -qE "^> Task :test FAILED$" /tmp/warmup.log; then
    echo "FATAL: :test FAILED on the unpatched tree" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

if ! grep -qE "^[^> ].* > .+ (PASSED|FAILED|SKIPPED)$" /tmp/warmup.log; then
    echo "FATAL: no per-case test lines -- init.gradle testLogging not in effect" >&2
    tail -60 /tmp/warmup.log >&2
    exit 1
fi

""".format(repo=self.pr.repo, sha=self.pr.base.sha, gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
""".format(repo=self.pr.repo)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(repo=self.pr.repo)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(repo=self.pr.repo)
                + _TEST_BODY.format(gradle_cmd=_GRADLE_CMD),
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_TASK_LINE = re.compile(r"^> Task (:\S+)(?:\s+(\S+))?\s*$")

_JUNIT_LINE = re.compile(r"^(\S.* > .+?) (PASSED|FAILED|SKIPPED)$")

_TASK_FAILED_SUFFIXES = {"FAILED"}
_TASK_SKIPPED_SUFFIXES = {"SKIPPED", "NO-SOURCE"}


def parse_gradle_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    for raw in clean.splitlines():
        line = raw.rstrip()

        m = _TASK_LINE.match(line)
        if m:
            name, suffix = m.group(1), m.group(2)
            if suffix in _TASK_FAILED_SUFFIXES:
                failed_tests.add(name)
            elif suffix in _TASK_SKIPPED_SUFFIXES:
                skipped_tests.add(name)
            else:
                passed_tests.add(name)
            continue

        if line.startswith("> "):
            continue

        m = _JUNIT_LINE.match(line)
        if m:
            name, status = m.group(1), m.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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


@Instance.register("selenide", "selenide")
class Selenide(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return SelenideImageDefault(self.pr, self._config)

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
        return parse_gradle_log(test_log)
