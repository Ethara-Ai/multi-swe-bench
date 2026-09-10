import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# jestjs/jest  --  phase-6, 10 PRs (14309 .. 15643), one SHARED base + N PR.
# ---------------------------------------------------------------------------
# Span 2023-07 .. 2025-06 (jest 29 -> jest 30). node:20 is the common
# denominator: jest 29 supports ^14||^16||>=18 and jest 30 supports
# ^18.14||^20||>=22, so node:20 runs every commit in the set -> ONE base.
#
# The base clones jest ONCE (full history) via `# syntax=docker/dockerfile:1.6`
# so the DockerfileEnhancer leaves the clone untouched (no per-commit history
# scrub) and every PR's base.sha stays reachable for `git checkout`. Cloning in
# the base (not per PR) also avoids the repeated large-clone that 429'd the old
# per-PR bucket design.
#
# RESOLUTION = SCOPED TEST RUN. `yarn jest` with no args runs jest's own
# ~5000-test self-suite, which in a container has ~950 baseline failures +
# ~1136 mismatched snapshots and swings +/- tens of tests per run -> no stable
# f2p/n2p signal (measured: 0/0 for every PR). Instead each graded stage runs
# ONLY the test files the PR's test.patch touches (derived at runtime, incl.
# __snapshots__/X.test.ts.snap -> X.test.ts; __typetests__ excluded -- those
# need a tsd runner, and every such PR also has a regular target). That isolates
# the graded transition from the self-suite noise (probe on pr-14895: the
# scoped target is 1 test, FAIL at test stage). `git clean -fd` before each
# stage removes the untracked fixture files a prior apply left behind (without
# it the fix-stage `git apply` fails "already exists").
_NODE_BASE = "node:20-bookworm"

# Shell snippet (reused verbatim by run/test/fix) that derives the scoped jest
# target files from /home/test.patch. Kept identical across the three graded
# stages so a FAIL->PASS can only come from the applied patch, not the command.
_TARGETS = (
    "TARGETS=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null "
    "| sed 's#^+++ b/##' "
    "| sed -E 's#__snapshots__/##; s#\\.snap$##' "
    "| grep -E '\\.(test|spec)\\.[jt]sx?$' "
    "| grep -v '__typetests__' | sort -u | tr '\\n' ' ')\n"
    'echo "SCOPED JEST TARGETS: $TARGETS"\n'
)

# Bring the working tree back to the pinned base commit before a stage. `-fd`
# (no -x) drops untracked files a previous apply added but keeps node_modules/
# and the packages/*/build output, so the warm install/build survives.
_RESET = "git reset --hard\ngit clean -fdq\n"

# install + build the toolchain the tests import. --immutable first (honours the
# committed yarn.lock); fall back to a mutable install when a patch touched a
# lockfile. build:js compiles TS->JS so jest can require the packages.
_SETUP = (
    "corepack enable || true\n"
    "yarn install --immutable || yarn install || true\n"
    "yarn build:js || yarn build || true\n"
)

# No `|| true` on the jest line: a non-zero exit is expected (the test stage is
# meant to fail) and the harness reads results from parse_log, not the exit
# code. `2>&1` keeps jest's verbose per-test ✓/✗ lines, which parse_log reads.
_JEST = "yarn jest $TARGETS --verbose 2>&1\n"


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

    def dependency(self) -> Union[str, "Image"]:
        return _NODE_BASE

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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN corepack enable || true

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}

CMD ["/bin/bash"]
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{self.pr.repo}\n"
                # ensure the pinned commit is present (full clone has it; fetch as a fallback)
                f"git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\\n"
                f"    || git fetch --no-tags --depth=2147483647 origin {self.pr.base.sha} \\\n"
                f'    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"\n'
                "git reset --hard\n"
                "git clean -fdq\n"
                f"git checkout --detach {self.pr.base.sha}\n"
                f'test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"\n'
                "git clean -fdq\n"
                + _SETUP,
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{self.pr.repo}\n"
                + _RESET
                + _SETUP
                + _TARGETS
                + _JEST,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{self.pr.repo}\n"
                + _RESET
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + _SETUP
                + _TARGETS
                + _JEST,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{self.pr.repo}\n"
                + _RESET
                + "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
                + _SETUP
                + _TARGETS
                + _JEST,
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("jestjs", "jest_15643_to_14309")
class JestPhase6(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_jest_log(test_log)


def _parse_jest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    passed_res = [
        re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
        re.compile(r"^\s*[✓✔]\s+(.+)$"),
    ]
    failed_res = [
        re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
        re.compile(r"^\s*[×✗✕]\s+(.+)$"),
    ]
    skipped_res = [
        re.compile(r"^\s*SKIP\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
        re.compile(r"^\s*[○✎]\s+(.+)$"),
    ]

    for line in test_log.splitlines():
        for pat in passed_res:
            m = pat.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
        for pat in failed_res:
            m = pat.match(line)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))
        for pat in skipped_res:
            m = pat.match(line)
            if m:
                skipped_tests.add(m.group(1))

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
