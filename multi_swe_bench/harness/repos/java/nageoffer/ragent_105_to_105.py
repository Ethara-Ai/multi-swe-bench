from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch_text: str) -> str:
    if not patch_text:
        return patch_text

    header = re.compile(r"(?m)^diff --git a/(.+?) b/(.+?)$")
    marks = [m.start() for m in header.finditer(patch_text)]
    if not marks:
        return patch_text

    marks.append(len(patch_text))
    kept = [patch_text[: marks[0]]]
    for index in range(len(marks) - 1):
        section = patch_text[marks[index] : marks[index + 1]]
        if "GIT binary patch" in section:
            continue
        if re.search(r"(?m)^Binary files .* differ$", section):
            continue
        if re.search(r"(?m)^diff --git a/(?:target|build|dist)/", section):
            continue
        kept.append(section)
    return "".join(kept)


class Ragent105To105ImageBase(Image):
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
        return "maven:3.9-eclipse-temurin-17"

    def image_tag(self) -> str:
        return "base-105-to-105"

    def workdir(self) -> str:
        return "base-105-to-105"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV MAVEN_OPTS="-Xmx3g -Dfile.encoding=UTF-8"

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

"""


class Ragent105To105ImageDefault(Image):
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
        return Ragent105To105ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{_sanitize_patch(self.pr.fix_patch)}",
            ),
            File(
                ".",
                "test.patch",
                f"{_sanitize_patch(self.pr.test_patch)}",
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
                "run_tests.sh",
                r"""#!/bin/bash
set -o pipefail

cd /home/{pr.repo}

export LC_ALL=C.UTF-8
export MAVEN_OPTS="-Xmx3g -Dfile.encoding=UTF-8"

mvn -B -ntp -pl framework,bootstrap -am test \
    -Dspotless.apply.skip=true \
    -Dspotless.check.skip=true \
    -Dmaven.test.failure.ignore=true \
    -DfailIfNoTests=false 2>&1 || true

echo "===== BEGIN TEST RESULTS ====="
find /home/{pr.repo} -path '*/target/surefire-reports/TEST-*.xml' -exec cat {{}} \; 2>/dev/null
echo ""
echo "===== END TEST RESULTS ====="

""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

mvn -B -ntp -pl framework,bootstrap -am test \\
    -Dspotless.apply.skip=true \\
    -Dspotless.check.skip=true \\
    -Dmaven.test.failure.ignore=true \\
    -DfailIfNoTests=false || true

find . -path '*/target/surefire-reports/TEST-*.xml' | grep -q .
find . -path '*/target/test-classes/*' -name '*.class' | grep -q .

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

echo DEPS_OK

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh

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

        if self.config.need_clone:
            clone = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{clone}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}
ARG BASE_COMMIT="{self.pr.base.sha}"
RUN git config --global --add safe.directory /home/{self.pr.repo}
{Image._HARDENING_BLOCK}
WORKDIR /home

{self.clear_env}

"""


@Instance.register("nageoffer", "ragent_105_to_105")
class RAGENT_105_TO_105(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Ragent105To105ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        testcase_re = re.compile(
            r'<testcase\b(?=[^>]*\bname="([^"]*)")(?=[^>]*\bclassname="([^"]*)")'
            r'[^>]*?(/>|>(.*?)</testcase>)',
            re.DOTALL,
        )

        for match in testcase_re.finditer(clean_log):
            name = match.group(1)
            classname = match.group(2)
            closing = match.group(3)
            inner = match.group(4) or ""
            test_id = f"{classname}.{name}"

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

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


Instance.register("nageoffer", "ragent")(RAGENT_105_TO_105)
