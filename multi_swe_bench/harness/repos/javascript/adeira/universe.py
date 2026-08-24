import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class UniverseImageBase(Image):
    """Toolchain + cloned source. Built before the PR image."""

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
        return "node:14-bullseye"

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

        # The lone `git clone` is deliberate: DockerfileEnhancer rewrites it
        # into the parameterized clone, WORKDIR, reset/checkout and hardening
        # block. The proxy/cert ENV and OCI labels are injected too.
        return f"""FROM {image_name}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
      git ca-certificates python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
"""


class UniverseImageDefault(Image):
    """Per-PR layer: patches, graded scripts, installed dependencies."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        # An Image, not a string, so the enhancer leaves the Dockerfile below
        # verbatim and this layer stays the thin patch/script layer it is.
        return UniverseImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # CI=true and --ci are correctness requirements, not convention: jest
    # writes MISSING inline snapshots as passes when CI is unset, which would
    # fabricate the two gold assertions. --verbose keeps per-test output (a
    # summary would parse as zero tests); --runInBand avoids worker deadlock;
    # 2>&1 because jest reports on stderr.
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
            # Clean-tree guard, called on both sides of the checkout.
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
            # The install must not end in `|| true`: a partial install would be
            # cached as a good layer and every graded run would fail unexplained.
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export CI=true

# 600000 was a PER-REQUEST timeout, not a total one, and it is where the
# 2026-08-21 multi-hour stall came from: one wedged socket burned 600s before
# yarn retried it, ten times over. 2 minutes is far above any healthy fetch.
export YARN_NETWORK_TIMEOUT=120000

# Concurrency is left at yarn's default of 8. The previous value of 1 existed
# only to nurse the QEMU-emulated linux/arm64 leg of a multi-arch build; on a
# native single-arch build it just serialises ~2000 tarball fetches for nothing.
# The build log for 2026-08-21 also shows concurrency=1 was already in force on
# the run that failed with ECONNREFUSED, so it never bought the reliability it
# was supposed to.

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Each attempt is bounded by `timeout 1800` so a wedged socket cannot outlive
# the attempt; timeout exits 124, which the loop treats like any other failure.
for attempt in 1 2 3; do
  echo "yarn install attempt $attempt"
  timeout 1800 yarn install --frozen-lockfile --non-interactive && break
  if [ "$attempt" = 3 ]; then
    echo "yarn install failed after 3 attempts" >&2
    exit 1
  fi
  sleep $((attempt * 30))
done

test -d node_modules/.bin || {{ echo "node_modules missing after install" >&2; exit 1; }}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npx jest --config=.jest.config.js --selectProjects @adeira/sx --verbose --runInBand --ci 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx jest --config=.jest.config.js --selectProjects @adeira/sx --verbose --runInBand --ci 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
npx jest --config=.jest.config.js --selectProjects @adeira/sx --verbose --runInBand --ci 2>&1

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

        # Not run through DockerfileEnhancer (dependency() is an Image), so this
        # layer must stand alone -- no ${BASE_COMMIT}/${REPO_URL}, no injected
        # ARGs. Proxy/cert ENV and the checked-out tree come from the base.
        return f"""FROM {name}:{tag}

{copy_commands}
# Bake node_modules into the image so the graded runs need no network.
{prepare_commands}
"""


@Instance.register("adeira", "universe")
class Universe(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UniverseImageDefault(self.pr, self._config)

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

        # jest --verbose prints an indent tree per suite; the header carries the
        # project displayName between status and path. Tests are keyed as
        #   <suite path>::<describe chain>::<test name>
        # so report.py's _test_name_matches_files can split on the first '::'
        # and match the head against the patched files. The displayName is
        # dropped -- only paths appear in a diff.
        ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        duration = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)$")
        header = re.compile(r"^(?:PASS|FAIL)\s+(?:\S+\s+)?(\S+)$")
        status_marks = {
            "✓": passed_tests,  # check mark
            "✔": passed_tests,  # heavy check mark
            "✕": failed_tests,  # multiplication x
            "✗": failed_tests,  # ballot x
            "×": failed_tests,  # multiplication sign
            "○": skipped_tests,  # white circle
            "◯": skipped_tests,  # large circle
        }

        # Suite file from the last PASS/FAIL header, duration stripped so the node
        # ID is stable. The project segment is optional: the failure summary
        # reprints bare 'FAIL <path>' headers.
        suite_file: Optional[str] = None
        # (indent, name) for each currently open describe level.
        describe_stack: list[tuple[int, str]] = []
        # Failure detail follows the test list and is indented like describes;
        # stop feeding the describe stack until the next header.
        in_failure_detail = False

        for raw_line in test_log.split("\n"):
            line = ansi.sub("", raw_line).rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith(("PASS ", "FAIL ")):
                match = header.match(duration.sub("", stripped))
                suite_file = match.group(1) if match else None
                describe_stack.clear()
                in_failure_detail = False
                continue

            if stripped.startswith("●"):  # black circle, failure heading
                in_failure_detail = True
                continue

            indent = len(line) - len(line.lstrip())
            mark = stripped[0]

            if mark in status_marks:
                name = duration.sub("", stripped[1:].strip())
                segments = [n for i, n in describe_stack if i < indent]
                segments.append(name)
                if suite_file:
                    segments.insert(0, suite_file)
                status_marks[mark].add("::".join(segments))
                continue

            if in_failure_detail:
                continue

            # Anything else inside a suite block is a describe() heading.
            while describe_stack and describe_stack[-1][0] >= indent:
                describe_stack.pop()
            describe_stack.append((indent, stripped))

        # Resolve any overlap in favour of the worse outcome: TestResult raises
        # ValueError on intersecting sets, which would abort the instance.
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
