import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ECR_PREFIX = "argos"


class ImageBase_9305_to_6444(Image):
    """Base image for syncthing PRs (#6444-#9305).

    Uses golang:1.23 — at or above the newest go.mod directive in this range
    (#9305 declares `go 1.22`), so GOTOOLCHAIN=local can pin the image's own
    toolchain. Letting GOTOOLCHAIN=auto download a toolchain instead fails on
    arm64 hosts with "exec go1.22.0: exec format error".
    golang:1.23 is based on Debian Bookworm (current).
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

    def dependency(self) -> Union[str, "Image"]:
        return "golang:1.23"

    def image_prefix(self) -> str:
        return ECR_PREFIX

    def image_name(self) -> str:
        return f"{self.image_prefix()}/{self.pr.org}__{self.pr.repo}".lower()

    def image_tag(self) -> str:
        return "base-go1_23"

    def workdir(self) -> str:
        return "base-go1_23"

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

{code}

{self.clear_env}

"""


class ImageDefault_9305_to_6444(Image):
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
        return ImageBase_9305_to_6444(self.pr, self.config)

    def image_prefix(self) -> str:
        return ECR_PREFIX

    def image_name(self) -> str:
        return f"{self.image_prefix()}/{self.pr.org}__{self.pr.repo}".lower()

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

git config --global advice.detachedHead false || true
git fetch --no-tags origin {pr.base.sha} 2>/dev/null || git fetch --no-tags https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
git checkout -f {pr.base.sha}

export GOFLAGS=-mod=mod
export GOTOOLCHAIN=local
go mod download >/dev/null 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
export PATH="/usr/local/go/bin:$PATH"
export GOFLAGS=-mod=mod
export GOTOOLCHAIN=local
go version

RAW=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '_test[.]go$' | xargs -r -n1 dirname | sort -u | grep -v '^vendor/' | grep -v '^Godeps/')
PKGS=""
for d in $RAW; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then PKGS="$PKGS ./$d"; fi
done
if [ -z "$PKGS" ]; then
  PKGS=$(find . -name "*_test.go" -not -path "./vendor/*" -not -path "./Godeps/*" | xargs -r -n1 dirname | sort -u | sed 's|^|./|' | tr '\\n' ' ')
fi
echo "RESOLVED_PKGS: $PKGS"

rc=0
for p in $PKGS; do
  go test -v -count=1 -timeout 180s "$p" < /dev/null || rc=1
done
exit $rc

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
export PATH="/usr/local/go/bin:$PATH"
export GOFLAGS=-mod=mod
export GOTOOLCHAIN=local
go version

if ! git apply /home/test.patch; then
  echo "Warning: git apply test.patch failed, retrying with --reject..."
  git apply --reject /home/test.patch 2>&1 || true
  find . -name '*.rej' -delete 2>/dev/null || true
fi

RAW=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '_test[.]go$' | xargs -r -n1 dirname | sort -u | grep -v '^vendor/' | grep -v '^Godeps/')
PKGS=""
for d in $RAW; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then PKGS="$PKGS ./$d"; fi
done
if [ -z "$PKGS" ]; then
  PKGS=$(find . -name "*_test.go" -not -path "./vendor/*" -not -path "./Godeps/*" | xargs -r -n1 dirname | sort -u | sed 's|^|./|' | tr '\\n' ' ')
fi
echo "RESOLVED_PKGS: $PKGS"

rc=0
for p in $PKGS; do
  go test -v -count=1 -timeout 180s "$p" < /dev/null || rc=1
done
exit $rc

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
export PATH="/usr/local/go/bin:$PATH"
export GOFLAGS=-mod=mod
export GOTOOLCHAIN=local
go version

if ! git apply /home/test.patch /home/fix.patch; then
  echo "Warning: git apply failed, retrying with --reject..."
  git apply --reject /home/test.patch 2>&1 || true
  git apply --reject /home/fix.patch 2>&1 || true
  find . -name '*.rej' -delete 2>/dev/null || true
fi

RAW=$(cat /home/test.patch /home/fix.patch 2>/dev/null | grep '^diff --git' | sed 's|diff --git a/||;s| b/.*||' | grep '_test[.]go$' | xargs -r -n1 dirname | sort -u | grep -v '^vendor/' | grep -v '^Godeps/')
PKGS=""
for d in $RAW; do
  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then PKGS="$PKGS ./$d"; fi
done
if [ -z "$PKGS" ]; then
  PKGS=$(find . -name "*_test.go" -not -path "./vendor/*" -not -path "./Godeps/*" | xargs -r -n1 dirname | sort -u | sed 's|^|./|' | tr '\\n' ' ')
fi
echo "RESOLVED_PKGS: $PKGS"

rc=0
for p in $PKGS; do
  go test -v -count=1 -timeout 180s "$p" < /dev/null || rc=1
done
exit $rc

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


@Instance.register("syncthing", "syncthing_9305_to_6444")
class Syncthing_9305_to_6444(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_9305_to_6444(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
