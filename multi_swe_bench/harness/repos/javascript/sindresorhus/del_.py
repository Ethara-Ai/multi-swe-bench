"""sindresorhus/del - Node 14 / AVA era.

Module is named ``del_`` rather than ``del`` because ``del`` is a Python
keyword: ``from ...sindresorhus.del import *`` is a SyntaxError. Only the
string in ``@Instance.register`` has to match the dataset's ``repo`` field.

Toolchain discovered from the repo at base commit 7c756b39, not assumed:

  package.json  "engines": {"node": ">=10"}
                "scripts": {"test": "xo && ava && tsd"}
                devDependencies: ava ^2.4.0, xo ^0.33.1, tsd ^0.13.1
  .npmrc        package-lock=false        -> no lockfile, so `npm install`
  CI            node-version: [14, 12, 10] -> 14 is the newest version CI runs

`npm test` is deliberately NOT used. It chains `xo && ava && tsd`, so a lint
finding from xo short-circuits the whole command and AVA never runs - the stage
would report zero tests for a reason that has nothing to do with the patch.
The test runner is invoked directly instead.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --tap because the default reporter emits Unicode marks (✔/✖) wrapped in ANSI
# colour codes, which is fragile to parse. TAP is plain ASCII with an explicit
# `ok N - name` / `not ok N - name` grammar and a `# SKIP` directive.
# --concurrency=1 because these tests create and delete real temp directories;
# parallel workers race each other and produce failures unrelated to the patch.
AVA_TAP = "npx ava --tap --concurrency=1"


class DelImageBase(Image):
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
        # The repo's own CI matrix is [14, 12, 10]; 14 is the newest it is known
        # to run on. ava 2.x and xo 0.33 predate modern Node, so a current LTS
        # image is not a safe substitute.
        return "node:14-bullseye"

    def image_tag(self) -> str:
        # Per-PR, not a shared "base". The hardening block that DockerfileEnhancer
        # injects detaches at ONE ${BASE_COMMIT} and prunes every other object, so
        # a shared tag would let whichever PR built first pin the commit - and
        # every other PR's base commit would already be gone from that image.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return self.image_tag()

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
# DEBIAN_FRONTEND and LANG are deliberately absent: DockerfileEnhancer already
# injects both (with the same values) in the block it adds after FROM, so
# repeating them here only duplicates lines in the generated Dockerfile.
# LC_ALL and CI are set because the enhancer sets neither - LC_ALL keeps
# collation deterministic, and CI=true stops any tool dropping into watch mode.
ENV LC_ALL=C.UTF-8
ENV CI=true
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class DelImageDefault(Image):
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
        return DelImageBase(self.pr, self._config)

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
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
# Assert the reset actually produced a clean tree. Without this the script only
# ASSUMES it worked; a stray modified file would flow into all three graded
# stages and quietly corrupt the comparison with nothing in the log to show why.
bash /home/check_git_changes.sh

git checkout {pr.base.sha}
# Assert again after the checkout - this is the exact state every graded run
# starts from, so it is worth proving rather than trusting.
bash /home/check_git_changes.sh

# node_modules is gitignored, so installing it does not dirty the tree checked
# above - which is why the install comes last. `|| true` because npm exits
# non-zero on optional-dependency noise (fsevents is darwin-only and always
# "fails" on linux) even when every real dependency installed correctly.
rm -rf node_modules
npm install || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{ava} 2>&1
""".format(pr=self.pr, ava=AVA_TAP),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch
{ava} 2>&1
""".format(pr=self.pr, ava=AVA_TAP),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch
{ava} 2>&1
""".format(pr=self.pr, ava=AVA_TAP),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # Generated from files() rather than hard-coded, so a file added there can
        # never be written into the build context yet left uncopied - which would
        # surface at build time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/{f.name}\n" for f in self.files())

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sindresorhus", "del")
class Del(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DelImageDefault(self.pr, self._config)

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
        """Parse `npx ava --tap` output.

        Captured verbatim from the container at base commit 7c756b39:

            TAP version 13
            # delete files - async
            ok 1 - delete files - async
            not ok 27 - onProgress option - progress of non-existent file
            1..29
            # tests 29
            # pass 26
            # fail 3

        Only `ok` / `not ok` lines are counted. Lines beginning with `#` are TAP
        comments - AVA emits one per test AND one per `beforeEach` hook, so
        treating them as results would roughly double every count and invent
        test names like "beforeEach hook for delete files - async".
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # `- ` separates the number from the description; the description itself
        # may contain " - " (e.g. "delete files - async"), so the split is
        # anchored to the leading `ok N - ` rather than done greedily.
        result_re = re.compile(r"^(ok|not ok)\s+(\d+)\s+-\s+(.*)$")
        # TAP marks a skip with a trailing directive: `ok 3 - name # SKIP why`
        skip_re = re.compile(r"^(ok|not ok)\s+(\d+)\s+-\s+(.*?)\s+#\s*(SKIP|TODO)\b.*$", re.I)

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        for raw_line in test_log.split("\n"):
            line = ansi_escape.sub("", raw_line).strip()
            if not line:
                continue

            # The TAP plan (`1..29`) and trailing `# tests/pass/fail` summary
            # carry no per-test information.
            if line.startswith("1.."):
                continue

            skip_match = skip_re.match(line)
            if skip_match:
                name = skip_match.group(3).strip()
                if name and name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                continue

            match = result_re.match(line)
            if not match:
                continue

            status, _, name = match.groups()
            name = name.strip()
            if not name:
                continue

            if status == "not ok":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            else:
                if name not in failed_tests:
                    skipped_tests.discard(name)
                    passed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )