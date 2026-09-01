import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "golang:1.15"

TOOLCHAIN_SETUP = r"""ENV GO111MODULE=on
ENV GOFLAGS=-mod=mod
ENV CGO_ENABLED=0
"""

TEST_CMD = "go test -v -count=1 -p 1 -timeout 1800s ./server/..."


class AgendaImageBase(Image):
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
        return BASE_IMAGE

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        sections = [f"FROM {image_name}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append("WORKDIR /home/")
        sections.append(TOOLCHAIN_SETUP.strip())
        sections.append(code)
        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


class AgendaImageDefault(Image):
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
        return AgendaImageBase(self.pr, self.config)

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
                "report-build-failures.sh",
                r"""#!/bin/bash
set -uo pipefail

log="${1:?usage: report-build-failures.sh <go-test-log>}"
[ -f "$log" ] || exit 0

MODULE=$(awk '/^module /{print $2; exit}' go.mod 2>/dev/null)
[ -n "$MODULE" ] || exit 0

grep -qE '^FAIL[[:space:]]+[^[:space:]]+[[:space:]]+\[build failed\]' "$log" || exit 0

baseline=/home/baseline-tests.txt

broken=$(grep -oE '^[^[:space:]:]+_test\.go:[0-9]+:[0-9]+:' "$log" \
         | cut -d: -f1 | sort -u)

tops=$(mktemp)
for f in $broken; do
  [ -f "$f" ] || continue
  grep -hoE '^func Test[A-Za-z0-9_]*\(' "$f" 2>/dev/null \
    | sed -E 's/^func //; s/\($//' >> "$tops"
done
sort -u -o "$tops" "$tops"

names=$(mktemp)
cat "$tops" > "$names"
if [ -s "$baseline" ]; then
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    grep -E "^${t}/" "$baseline" >> "$names" 2>/dev/null
  done < "$tops"
fi
sort -u -o "$names" "$names"

salvage=$(mktemp)
if [ -s "$tops" ]; then
  echo "=== SALVAGE: the package failed to build. Removing only the un-compilable"
  echo "=== SALVAGE: test file(s) -- $(echo $broken) -- and re-running so every test"
  echo "=== SALVAGE: that does compile reports its real result. The removed file's"
  echo "=== SALVAGE: tests cannot run and are attributed FAIL after the salvage run."
  for f in $broken; do
    rm -f "$f"
  done
  ${TEST_CMD:-go test -v -count=1 ./...} > "$salvage" 2>&1
  cat "$salvage"
  echo "=== SALVAGE: end of salvage run"
fi

seen=$(mktemp)
cat "$log" "$salvage" 2>/dev/null \
  | grep -oE '^[[:space:]]*--- (PASS|FAIL|SKIP): [^[:space:]]+' \
  | awk '{print $3}' | sort -u > "$seen"

emitted=0
while IFS= read -r name; do
  [ -n "$name" ] || continue
  if [ "$name" = "TestMain" ]; then
    continue
  fi
  if grep -Fxq "$name" "$seen"; then
    continue
  fi
  if [ "$emitted" -eq 0 ]; then
    echo "=== SYNTHETIC: the tests below are declared in a test file that does not"
    echo "=== SYNTHETIC: compile at this stage, so go test never ran them. They are"
    echo "=== SYNTHETIC: attributed FAIL; subtest names come from $baseline."
    emitted=1
  fi
  echo "--- FAIL: $name (0.00s)"
done < "$names"

if [ "$emitted" -eq 1 ]; then
  echo "=== SYNTHETIC: end of synthesized results"
fi

rm -f "$tops" "$names" "$salvage" "$seen"
exit 0

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
if ! git rev-parse --verify -q {pr.base.sha}^{{commit}} > /dev/null; then
  git remote add origin https://github.com/{pr.org}/{pr.repo}.git
  git fetch --depth=1 origin {pr.base.sha}
  git remote remove origin
fi
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || echo "prepare.sh: go mod download reported errors; the warm-up below is the gate."

: > /home/baseline-tests.txt
if [ "$(uname -m)" = "x86_64" ]; then
  {test_cmd} > /tmp/baseline.log 2>&1 || true
  cat /tmp/baseline.log
  grep -oE '^[[:space:]]*--- (PASS|FAIL|SKIP): [^[:space:]]+' /tmp/baseline.log \
    | awk '{{print $3}}' | sort -u > /home/baseline-tests.txt
  count=$(wc -l < /home/baseline-tests.txt)
  echo "prepare.sh: recorded $count baseline test names."
  if [ "$count" -eq 0 ]; then
    echo "prepare.sh: the baseline warm-up produced no test names. The image would" >&2
    echo "prepare.sh: grade with an empty inventory, so the build fails here instead." >&2
    exit 1
  fi
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

git checkout -- .
bash /home/check_git_changes.sh

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_CMD="{test_cmd}"

cd /home/{pr.repo}

set +e
{test_cmd} 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_CMD="{test_cmd}"

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --exclude='*.png' /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

set +e
{test_cmd} 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export TEST_CMD="{test_cmd}"

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn --exclude='*.png' /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

set +e
{test_cmd} 2>&1 | tee /tmp/go-test.log
status=${{PIPESTATUS[0]}}
set -e

bash /home/report-build-failures.sh /tmp/go-test.log
exit "$status"

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


@Instance.register("mattermost-community", "mattermost-plugin-agenda")
class MattermostPluginAgenda(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AgendaImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        run_tests = set(re.findall(r"=== RUN\s+(\S+)", test_log))
        passed_tests = set(re.findall(r"--- PASS: (\S+)", test_log))
        failed_tests = set(re.findall(r"--- FAIL: (\S+)", test_log))
        skipped_tests = set(re.findall(r"--- SKIP: (\S+)", test_log))

        unterminated = run_tests - passed_tests - failed_tests - skipped_tests
        failed_tests |= unterminated

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
