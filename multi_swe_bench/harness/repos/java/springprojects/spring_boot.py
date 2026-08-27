import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SpringBootImageBase(Image):
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
        return "eclipse-temurin:25-jdk"

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
ENV GRADLE_OPTS="-Xmx4g -Dfile.encoding=UTF-8 -Dorg.gradle.daemon=false"
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SpringBootImageDefault(Image):
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
        return SpringBootImageBase(self.pr, self._config)

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
                "print_test_results.sh",
                """#!/bin/bash
cd /home/{repo} || exit 0
echo "===== BEGIN TEST RESULTS ====="
find . -path '*/build/test-results/test/TEST-*.xml' -print0 2>/dev/null \\
  | while IFS= read -r -d '' f; do
      rel="${{f#./}}"
      module="${{rel%%/build/test-results/test/*}}"
      cls=$(grep -o '<testsuite[^>]*' "$f" | head -1 | sed -n 's/.*[[:space:]]name="\\([^"]*\\)".*/\\1/p')
      if [ -z "$cls" ]; then
        cls="${{rel##*/TEST-}}"
        cls="${{cls%.xml}}"
      fi
      cls="${{cls%%\\$*}}"
      path="${{cls//.//}}"
      src=""
      for probe in java:java kotlin:kt; do
        root="${{probe%%:*}}"
        ext="${{probe##*:}}"
        if [ -f "$module/src/test/$root/$path.$ext" ]; then
          src="$module/src/test/$root/$path.$ext"
          break
        fi
      done
      [ -n "$src" ] || src="$module/src/test/java/$path.java"
      echo "##### FILE: $src"
      cat "$f"
      echo
    done
echo "===== END TEST RESULTS ====="
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e {sha}^{{commit}} 2>/dev/null; then
    git fetch --depth 1 https://github.com/{org}/{repo}.git {sha}
fi
git checkout {sha}
bash /home/check_git_changes.sh

chmod +x ./gradlew || true

export CI=false
export ENABLE_PREDICTIVE_TEST_SELECTION=false
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"
timeout --kill-after=60 1800 ./gradlew :core:spring-boot:testClasses --no-daemon --max-workers=4 || true
""".format(repo=self.pr.repo, sha=self.pr.base.sha, org=self.pr.org),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=false
export ENABLE_PREDICTIVE_TEST_SELECTION=false
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
rc=0
timeout --kill-after=60 3000 ./gradlew :core:spring-boot:test --no-daemon --continue --max-workers=4 || rc=$?
bash /home/print_test_results.sh
exit $rc
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=false
export ENABLE_PREDICTIVE_TEST_SELECTION=false
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn --check /home/test.patch
git apply --whitespace=nowarn /home/test.patch
rc=0
timeout --kill-after=60 3000 ./gradlew :core:spring-boot:test --no-daemon --continue --max-workers=4 || rc=$?
bash /home/print_test_results.sh
exit $rc
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=false
export ENABLE_PREDICTIVE_TEST_SELECTION=false
export GRADLE_OPTS="$GRADLE_OPTS -Dorg.gradle.configuration-cache=false -Dorg.gradle.caching=false"

cd /home/{repo}
git apply --whitespace=nowarn --check /home/test.patch /home/fix.patch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
rc=0
timeout --kill-after=60 3000 ./gradlew :core:spring-boot:test --no-daemon --continue --max-workers=4 || rc=$?
bash /home/print_test_results.sh
exit $rc
""".format(repo=self.pr.repo),
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


@Instance.register("spring-projects", "spring-boot")
class SpringBoot(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SpringBootImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        marker = re.compile(r"^##### FILE: (\S+)[ \t]*$", re.M)
        chunks = marker.split(clean)[1:]

        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        name_re = re.compile(r'\bname="([^"]*)"')
        classname_re = re.compile(r'\bclassname="([^"]*)"')

        for source_file, body in zip(chunks[0::2], chunks[1::2]):
            for tc in testcase_re.finditer(body):
                attrs = tc.group(1) or ""
                closing = tc.group(2)
                inner = tc.group(3) or ""

                nm = name_re.search(attrs)
                if not nm:
                    continue

                cn = classname_re.search(attrs)
                nested = ""
                if cn and "$" in cn.group(1):
                    nested = cn.group(1).split("$", 1)[1].replace("$", ".") + "."

                test_id = f"{source_file}::{nested}{nm.group(1)}"

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
