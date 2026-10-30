import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



def _test_patch_java_files(test_patch: str) -> list[str]:
    """The .java files the test patch touches."""
    return sorted(
        {
            line.split()[3][2:]
            for line in test_patch.splitlines()
            if line.startswith("diff --git ") and line.split()[3].endswith(".java")
        }
    )


class JanusgraphImageBase(Image):
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
        return "maven:3.9.9-eclipse-temurin-17"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class JanusgraphImageDefault(Image):
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
        return JanusgraphImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:

        mvn = (
            'mvn -B -fae test -pl "$(cat /home/test_modules)" -am '
            "-DfailIfNoTests=false -Dsurefire.failIfNoSpecifiedTests=false "
            "-Dmaven.test.failure.ignore=true "
            "-Dsurefire.useFile=false -Dsurefire.reportFormat=plain"
        )
        test_cmd = f'{mvn} -Dtest="$(cat /home/test_classes)"'

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
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

# Resolve the test patch's .java files into "<module> <TestClass>" pairs.
resolve() {{
  local file="$1" depth="$2" cls sub
  cls=$(basename "$file" .java)
  if [ -f "$file" ] && grep -qE "abstract[[:space:]]+class[[:space:]]+$cls\\b" "$file"; then
    if [ "$depth" -eq 0 ] && ! grep -qE "@(Test|ParameterizedTest|RepeatedTest|TestFactory)\\b" "$file"; then
      return 0
    fi
    for sub in $(grep -rlE "extends[[:space:]]+$cls\\b" --include='*.java' .); do
      resolve "${{sub#./}}" $((depth + 1))
    done
    return 0
  fi
  case "$file" in */src/test/java/*) ;; *) return 0;; esac
  case "$cls" in Test*|*Test|*Tests|*TestCase) ;; *) return 0;; esac
  if [ -f "$file" ] && grep -q "@Testcontainers" "$file"; then return 0; fi
  echo "${{file%%/src/test/java/*}} $cls"
}}
for file in {test_files}; do resolve "$file" 0; done | sort -u > /home/test_selection
cut -d' ' -f1 /home/test_selection | sort -u | paste -sd, - > /home/test_modules
cut -d' ' -f2 /home/test_selection | sort -u | paste -sd, - > /home/test_classes
test -s /home/test_classes
echo "modules: $(cat /home/test_modules)"
echo "tests:   $(cat /home/test_classes)"

# Compiles every module the stages build and fills ~/.m2. Every test class but no
# method matches, so each module forks its test JVM (resolving surefire's JUnit
# provider) without running a test.
{mvn} -Dtest='*Test*#__no_such_test__'
""".format(
                pr=self.pr,
                mvn=mvn,
                test_files=" ".join(_test_patch_java_files(self.pr.test_patch)),
            ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(pr=self.pr, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.replace(" --aggressive", "").rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("JanusGraph", "janusgraph")
class Janusgraph(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return JanusgraphImageDefault(self.pr, self._config)

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
            ansi_escape_pattern = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
            return ansi_escape_pattern.sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)


        result_pattern = re.compile(
            r"^\[\w+\]\s+([\w$.]+)\.([\w$]+(?:\([^)]*\))?(?:\[\d+\])?)"
            r"(?: -- Time elapsed: \S+ s( <<< (?:FAILURE|ERROR)!)?| (skipped))\s*$"
        )

        for line in test_log.splitlines():
            match = result_pattern.match(line)
            if not match:
                continue
            test_name = f"{match.group(1)}#{match.group(2)}"
            if match.group(4):
                skipped_tests.add(test_name)
            elif match.group(3):
                failed_tests.add(test_name)
            else:
                passed_tests.add(test_name)

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
