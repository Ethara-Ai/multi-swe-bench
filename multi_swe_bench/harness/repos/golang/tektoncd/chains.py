from __future__ import annotations

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# go.mod declares `go 1.16`. The sibling tektoncd config for this era
# (pipeline_go1_16.py) uses the same runtime and the same -mod=vendor flag.
GO_IMAGE = "golang:1.16-bullseye"

# ONE command, reached by run.sh / test-run.sh / fix-run.sh through
# run_tests.sh, so the three graded stages cannot drift apart.
#   -mod=vendor : the repo ships a populated vendor/ tree; using it keeps the
#                 build hermetic and needs no module downloads at all.
#   -json       : machine-readable events (never parse `go test` console text).
#   -count=1    : defeat the test cache so every stage genuinely re-runs.
# NOTE: test/e2e_test.go carries `//go:build e2e`, so the Kubernetes end-to-end
# tests are excluded automatically. No -tags e2e is passed and none should be:
# those tests create Tasks/TaskRuns against a live Tekton cluster.
TEST_CMD = (
    "go test -json -count=1 -mod=vendor -timeout 20m ./... > /home/results.jsonl"
)

BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

PARSE_GOTEST_PY = '''import json
import os

PATH = "/home/results.jsonl"

# `go test -json` emits one JSON object per line. A per-test verdict is an
# event carrying BOTH "Test" and a terminal Action. Package-level events (no
# "Test" key) are ignored - a package that fails to COMPILE emits no test
# events at all, which correctly yields NONE for its tests rather than a
# fabricated FAIL.
STATUS = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}

if os.path.exists(PATH):
    with open(PATH, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue

            test = ev.get("Test")
            action = ev.get("Action")
            if not test or action not in STATUS:
                continue

            pkg = ev.get("Package") or "?"
            # Subtests arrive as "TestFoo/sub"; keep the full path so ids stay
            # unique and rerunnable across packages.
            print("TESTCASE " + pkg + "::" + test + " " + STATUS[action])
'''

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""


class ImageBase(Image):
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
        return GO_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # Per-PR, never shared: the injected hardening block checks out
        # ${BASE_COMMIT} then deletes every git ref, so a shared base tag would
        # stay pinned to whichever PR built it first.
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

        # Do NOT set DEBIAN_FRONTEND or LANG - DockerfileEnhancer injects both.
        # ca-certificates is listed explicitly rather than inherited silently,
        # so TLS trust is not an unstated dependency on the base image.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
 && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "parse_gotest.py", PARSE_GOTEST_PY),
            File(
                ".",
                "print_test_detail.sh",
                "#!/bin/bash\n"
                f'echo "{BEGIN_MARKER}"\n'
                "python3 /home/parse_gotest.py\n"
                f'echo "{END_MARKER}"\n',
            ),
            File(
                ".",
                "run_tests.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                "export CI=true\n"
                "\n"
                f"cd /home/{repo}\n"
                "\n"
                "# never inherit the previous stage's results\n"
                "rm -f /home/results.jsonl\n"
                "\n"
                # -e is lifted only around the test call: at the test stage the
                # suite is SUPPOSED to fail, and dying here would report zero
                # tests and satisfy report.py's "fix something" check vacuously.
                "set +e\n"
                f"{TEST_CMD}\n"
                "RC=$?\n"
                "set -e\n"
                'echo "TEST_EXIT_CODE=$RC"\n'
                "\n"
                "bash /home/print_test_detail.sh\n",
            ),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                f"cd /home/{repo}\n"
                "git reset --hard\n"
                "git clean -fdx\n"
                "bash /home/check_git_changes.sh\n"
                f"git checkout {sha}\n"
                "bash /home/check_git_changes.sh\n"
                "\n"
                "go version\n"
                # vendor/ is committed, so there is nothing to download and the
                # build is hermetic. No `go mod download` is needed or wanted.
                'test -d vendor && echo "vendor/ present"\n'
                "\n"
                # HARD GATE - not tolerant. Warm the cache with a COMPILE-only
                # step (never a test run, which would bake results into the
                # image): `-run ^$` builds every test binary and executes none.
                "go build -mod=vendor ./...\n"
                "go test -count=1 -mod=vendor -run '^$' ./... > /dev/null\n"
                "\n"
                'echo "DEPS_OK"\n',
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "bash /home/run_tests.sh\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch; then\n"
                '    echo "Error: git apply test.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "bash /home/run_tests.sh\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                '    echo "Error: git apply test.patch+fix.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n"
                "bash /home/run_tests.sh\n",
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


@Instance.register("tektoncd", "chains")
class Chains(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return ImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)

        # Greedy id capture with the status as the FINAL token; Go subtest
        # names can contain spaces, which a \S+ capture would truncate.
        case_re = re.compile(r"^TESTCASE (.+) (PASSED|FAILED|SKIPPED)\s*$")

        in_detail = False
        for line in test_log.splitlines():
            stripped = line.strip()

            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            # Everything outside the markers is raw go/build output and is
            # ignored, so compile-error spew cannot pollute a test id.
            if not in_detail:
                continue

            m = case_re.match(stripped)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Failure wins; each test lands in exactly one bucket.
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
