import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class LazygitImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the runtime helper scripts + patches into /home/ and
        # warms the Go module + build caches so the eval scripts run offline.
        # The copied files live outside /home/{repo}, so the hardening pass
        # (which only operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
                "prepare.sh",
                """#!/bin/bash
# Warm the Go module + build caches at image-build time so the eval runs don't
# need network. The repo is already checked out at ${{BASE_COMMIT}} and hardened
# by Image.dockerfile(), so this script no longer performs any git checkout
# itself. Builds are allowed to fail (|| true) because their only purpose here
# is to populate caches; the real pass/fail signal comes from the
# run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true

# Ensure go.mod exists for old PRs that used dep/Gopkg.toml
if [ ! -f go.mod ]; then
  echo 'module github.com/jesseduffield/lazygit' > go.mod
  echo '' >> go.mod
  echo 'go 1.14' >> go.mod
fi

if [ -d vendor ]; then
  # Vendor directory exists — use it directly
  go build -mod=vendor ./... 2>&1 || true
else
  # No vendor — use module mode
  go mod download -x 2>&1 || true
  go build -mod=mod ./... 2>&1 || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Ensure go.mod exists for old PRs
if [ ! -f go.mod ]; then
  echo 'module github.com/jesseduffield/lazygit' > go.mod
  echo '' >> go.mod
  echo 'go 1.14' >> go.mod
fi

if [ -d vendor ]; then
  go test -v -mod=vendor -count=1 $(go list -mod=vendor ./... | grep -v integration)
else
  go test -v -mod=mod -count=1 $(go list -mod=mod ./... | grep -v integration)
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Reset go.mod/go.sum to clean state before applying patches
# For tracked files: restore from git. For synthetic ones: remove them.
git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

# Apply test patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

# Ensure go.mod exists (if patch didn't create one)
if [ ! -f go.mod ]; then
  echo 'module github.com/jesseduffield/lazygit' > go.mod
  echo '' >> go.mod
  echo 'go 1.14' >> go.mod
fi

if [ -d vendor ]; then
  go test -v -mod=vendor -count=1 $(go list -mod=vendor ./... | grep -v integration)
else
  # Delete go.sum to avoid checksum mismatch on republished packages
  rm -f go.sum

  # Fix Sirupsen/logrus case mismatch (old repos use capital S)
  go mod edit -replace github.com/Sirupsen/logrus=github.com/sirupsen/logrus@v1.9.4 2>/dev/null || true
  # Also fix imports in source (go mod tidy scans imports fresh)
  find . -name '*.go' -exec sed -i 's|github.com/Sirupsen/logrus|github.com/sirupsen/logrus|g' {{}} + 2>/dev/null || true

  # Fix go-i18n v2 DefaultLanguage (unexported in v2.6+, exported in v2.1.2)
  go mod edit -require github.com/nicksnyder/go-i18n/v2@v2.1.2 2>/dev/null || true

  # Bypass checksum verification for republished packages (go-getter)
  export GONOSUMCHECK='*'
  export GONOSUMDB='*'
  export GOFLAGS='-mod=mod'

  # Tidy modules after patch (may add new deps)
  go mod tidy 2>&1 || true

  go test -v -mod=mod -count=1 $(go list -mod=mod ./... | grep -v integration)
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Reset go.mod/go.sum to clean state before applying patches
# For tracked files: restore from git. For synthetic ones: remove them.
git checkout -- go.mod go.sum 2>/dev/null || rm -f go.mod go.sum

# Apply test patch then fix patch
git apply --whitespace=nowarn /home/test.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/test.patch 2>&1 || true

git apply --whitespace=nowarn /home/fix.patch 2>&1 || \\
  git apply --whitespace=nowarn --reject /home/fix.patch 2>&1 || true

# Ensure go.mod exists (if patches didn't create one)
if [ ! -f go.mod ]; then
  echo 'module github.com/jesseduffield/lazygit' > go.mod
  echo '' >> go.mod
  echo 'go 1.14' >> go.mod
fi

if [ -d vendor ]; then
  go test -v -mod=vendor -count=1 $(go list -mod=vendor ./... | grep -v integration)
else
  # Delete go.sum to avoid checksum mismatch on republished packages
  rm -f go.sum

  # Fix Sirupsen/logrus case mismatch (old repos use capital S)
  go mod edit -replace github.com/Sirupsen/logrus=github.com/sirupsen/logrus@v1.9.4 2>/dev/null || true
  # Also fix imports in source (go mod tidy scans imports fresh)
  find . -name '*.go' -exec sed -i 's|github.com/Sirupsen/logrus|github.com/sirupsen/logrus|g' {{}} + 2>/dev/null || true

  # Fix go-i18n v2 DefaultLanguage (unexported in v2.6+, exported in v2.1.2)
  go mod edit -require github.com/nicksnyder/go-i18n/v2@v2.1.2 2>/dev/null || true

  # Bypass checksum verification for republished packages (go-getter)
  export GONOSUMCHECK='*'
  export GONOSUMDB='*'
  export GOFLAGS='-mod=mod'

  # Tidy modules after patches (may add new deps)
  go mod tidy 2>&1 || true

  go test -v -mod=mod -count=1 $(go list -mod=mod ./... | grep -v integration)
fi

""".format(pr=self.pr),
            ),
        ]


@Instance.register("jesseduffield", "lazygit_5487_to_12")
class Lazygit(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LazygitImageDefault(self.pr, self._config)

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

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [
            re.compile(r"^\s*--- PASS: (\S+)"),
            re.compile(r"^ok\s+(\S+)\s+"),
        ]
        re_fail_tests = [
            re.compile(r"^\s*--- FAIL: (\S+)"),
            re.compile(r"^FAIL\s+(\S+)"),
        ]
        re_skip_tests = [
            re.compile(r"^\s*--- SKIP: (\S+)"),
        ]

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass in re_pass_tests:
                match = re_pass.match(line)
                if match:
                    passed_tests.add(match.group(1))

            for re_fail in re_fail_tests:
                match = re_fail.match(line)
                if match:
                    failed_tests.add(match.group(1))

            for re_skip in re_skip_tests:
                match = re_skip.match(line)
                if match:
                    skipped_tests.add(match.group(1))

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
