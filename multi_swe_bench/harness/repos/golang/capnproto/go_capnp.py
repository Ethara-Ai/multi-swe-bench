from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# go.mod declares `go 1.19` and .github/workflows/go.yml runs a 1.19/1.20
# matrix, so 1.19 is the era-correct floor.
GO_IMAGE = "golang:1.19-bullseye"

# ONE command, reached by run.sh / test-run.sh / fix-run.sh through
# run_tests.sh, so the three graded stages cannot drift apart.
#   -json    : machine-readable events (never parse `go test` console text)
#   -count=1 : defeat the test cache so every stage genuinely re-runs
TEST_CMD = "go test -json -count=1 -timeout 20m ./... > /home/results.jsonl"

BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

# ---------------------------------------------------------------------------
# DATASET DEFECT WORKAROUND (config-only; the .jsonl is NOT modified).
#
# test_patch encodes capnpc-go/testdata/persistent-simple.capnp.out as a
# stat-style placeholder carrying ZERO bytes:
#
#     new file mode 100644
#     index 00000000..686d7b90
#     Binary files /dev/null and b/...persistent-simple.capnp.out differ
#
# `git apply` refuses the whole patch over it ("cannot apply binary patch
# without full index line"), so the PR cannot even reach the test stage.
#
# That file is load-bearing: TestPersistent reads it as its golden input, so
# merely excluding it makes the test fail at BOTH the test and fix stages and
# yields zero credited tests.
#
# Fix: exclude only the malformed section from `git apply`, then materialise
# the real 2072 bytes from the base64 below. The payload is the genuine
# upstream blob 686d7b90263676a2f80f67e9a9d12db7e209d461 - the exact object
# the dataset's own index line names - and every stage asserts that hash after
# decoding, so a corrupted payload fails loudly instead of silently.
GOLDEN_PATH = "capnpc-go/testdata/persistent-simple.capnp.out"
GOLDEN_SHA = "686d7b90263676a2f80f67e9a9d12db7e209d461"
GOLDEN_B64 = """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
"""

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
            # unique and rerunnable.
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
        return "envagent"

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

        # Shared by test-run.sh and fix-run.sh so both graded stages stage the
        # golden file identically.
        apply_golden = (
            f"base64 -d /home/persistent_golden.b64 > {GOLDEN_PATH}\n"
            f"ACTUAL=$(git hash-object {GOLDEN_PATH})\n"
            f'if [ "$ACTUAL" != "{GOLDEN_SHA}" ]; then\n'
            f'    echo "FATAL: golden blob mismatch: got $ACTUAL want {GOLDEN_SHA}" >&2\n'
            "    exit 1\n"
            "fi\n"
            f'echo "GOLDEN_OK {GOLDEN_SHA}"\n'
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "persistent_golden.b64", GOLDEN_B64),
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
                "go mod download\n"
                "\n"
                # HARD GATE - not tolerant. Warm the cache with a COMPILE-only
                # step (never a test run, which would bake results into the
                # image): `-run ^$` builds every test binary and executes none.
                "go build ./...\n"
                "go test -count=1 -run '^$' ./... > /dev/null\n"
                "\n"
                # Prove the embedded payload is intact at BUILD time, so a
                # corrupt blob fails here rather than three stages later.
                "base64 -d /home/persistent_golden.b64 > /tmp/golden.check\n"
                "CHECK=$(git hash-object /tmp/golden.check)\n"
                f'if [ "$CHECK" != "{GOLDEN_SHA}" ]; then\n'
                '    echo "FATAL: embedded golden blob is corrupt: $CHECK" >&2\n'
                "    exit 1\n"
                "fi\n"
                "rm -f /tmp/golden.check\n"
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
                # --exclude skips ONLY the malformed zero-byte binary section;
                # every other hunk applies normally. Because --exclude makes
                # `git apply` succeed while deliberately skipping a file, the
                # hash assert below is what actually proves the file is staged.
                f"if ! git apply --whitespace=nowarn --exclude='{GOLDEN_PATH}' /home/test.patch; then\n"
                '    echo "Error: git apply test.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n" + apply_golden + "bash /home/run_tests.sh\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -eo pipefail\n"
                "\n"
                f"cd /home/{repo}\n"
                f"if ! git apply --whitespace=nowarn --exclude='{GOLDEN_PATH}' /home/test.patch /home/fix.patch; then\n"
                '    echo "Error: git apply test.patch+fix.patch failed" >&2\n'
                "    exit 1\n"
                "fi\n" + apply_golden + "bash /home/run_tests.sh\n",
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


@Instance.register("capnproto", "go-capnp")
class CAPNPROTO_GO_CAPNP(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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
