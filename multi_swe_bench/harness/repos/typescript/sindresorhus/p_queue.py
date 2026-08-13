"""sindresorhus/p-queue harness config — AVA + ts-node, npm, TypeScript.

Pinned from the repo at the PR's base commit:

* node 12          (newest entry in ``.travis.yml``; ``engines`` only says >=8)
* ava ^2.0.0       run through ``ts-node/register`` (see the ``ava`` block in
                   package.json: ``extensions: [ts]``, ``files: [test/**]``)
* typescript 3.8.3
* no lockfile      — neither package-lock.json nor yarn.lock exists, so plain
                   ``npm install`` is the only correct install command

Two deliberate departures from ``npm test``
-------------------------------------------
``npm test`` is ``xo && npm run build && nyc ava``. Neither prefix is used here:

* **xo** is a linter. A lint error would abort the script before AVA ever runs,
  turning a style nit into "zero tests captured" for the whole stage.
* **npm run build** (``del dist && tsc``) is unnecessary: ``test/test.ts`` does
  ``import PQueue from '../source'``, so ts-node compiles the sources in
  process. Building would also make ``tsc`` the thing that fails on the type
  error described below, rather than the tests.

``nyc`` is dropped too — coverage adds nothing to pass/fail classification.

Why TS_NODE_TRANSPILE_ONLY is set
---------------------------------
The fix patch widens ``EventEmitter<'active' | 'idle'>`` to
``EventEmitter<'active' | 'idle' | 'add' | 'next'>``. The tests the test patch
adds call ``queue.on('add', ...)`` and ``queue.on('next', ...)``, so **before**
the fix patch they are a TypeScript *type* error, not a failing assertion.

With ts-node type-checking on, that error takes down the whole of test.ts: AVA
reports a file-level error, emits no test titles, and every test in the file
lands as NONE for that stage. The instance would still validate — report.py
counts ``test != PASS -> fix == PASS`` — but the two new tests would be graded
without ever having run.

Transpiling instead lets the file load, so the new tests execute and fail on
their assertions (``timesCalled`` stays 0). That produces a clean, properly
identified FAIL -> PASS transition, which is what the benchmark is measuring.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Shared by run.sh / test-run.sh / fix-run.sh. The three stages MUST invoke the
# runner identically -- any divergence makes their results incomparable and
# silently breaks fail-to-pass detection.
RUN_TESTS = """\
export CI=true
# See the module docstring: without this the pre-fix type error hides every
# test in test/test.ts instead of letting the new ones fail on their assertions.
export TS_NODE_TRANSPILE_ONLY=true

# AVA is invoked directly rather than through `npm test`, which would run xo
# (a linter) and tsc first and abort the stage before any test ran.
npx --no-install ava --tap 2>&1 || true
"""


def _script(body: str, repo: str, base_sha: str = "") -> str:
    """Fill the placeholders.

    Plain replacement rather than str.format, so the shell's own ``${...}`` and
    ``$(...)`` need no brace escaping. The block placeholder expands first in
    case it ever carries placeholders of its own.
    """
    return (
        body.replace("[[RUN_TESTS]]", RUN_TESTS)
        .replace("[[REPO]]", repo)
        .replace("[[BASE_SHA]]", base_sha)
    )


class PQueueImageBase(Image):
    """Base image: node 12 with the repo cloned at the PR's base commit.

    No apt-get here, deliberately. The official node images are buildpack-deps
    derived and already ship git, and node:12 is Debian buster, whose apt
    repositories have been archived -- an ``apt-get update`` would fail.
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
        return "node:12"

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # DockerfileEnhancer rewrites the clone/COPY line into a standardized
        # ${REPO_URL} clone + ${BASE_COMMIT} checkout and appends the hardening
        # block, which strips the origin remote and every ref except the
        # detached HEAD. Nothing downstream may assume a remote exists.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class PQueueImageDefault(Image):
    """PR layer: patches, dependency install, and the three stage scripts."""

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
        return PQueueImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                _script(
                    """\
#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
git checkout [[BASE_SHA]]
bash /home/check_git_changes.sh

# Do NOT add `git fetch origin` here. The base image is hardened by
# DockerfileEnhancer, which removes the origin remote entirely, so a fetch
# would abort this script under `set -e`. The base image is already at
# BASE_COMMIT, so the checkout above is sufficient.

export CI=true

# The repo ships no lockfile, so `npm ci` is not usable.
npm install
""",
                    repo,
                    base_sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

[[RUN_TESTS]]""",
                    repo,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch

# The test patch can touch dependencies.
npm install || true

[[RUN_TESTS]]""",
                    repo,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _script(
                    """\
#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# The fix patch can touch dependencies.
npm install || true

[[RUN_TESTS]]""",
                    repo,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("sindresorhus", "p-queue")
class SindresorhusPQueue(Instance):
    """Harness instance for sindresorhus/p-queue — AVA via ts-node."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PQueueImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        """Parse AVA's TAP output, falling back to its default reporter."""
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        if not self._parse_tap(log, passed, failed, skipped):
            self._parse_console(log, passed, failed, skipped)

        # A title can be reported more than once (AVA retries a failing file, or
        # two files share a title). Failure wins over pass, and both win over
        # skip, so the three sets stay disjoint -- TestResult raises if they
        # overlap.
        passed -= failed
        skipped -= passed
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )

    # TAP 13, as emitted by `ava --tap`:
    #     ok 1 - .add()
    #     not ok 2 - should emit add event when adding task
    #     ok 3 - some pending test # SKIP
    _TAP_OK = re.compile(r"^ok\s+\d+\s*-?\s*(.*)$")
    _TAP_NOT_OK = re.compile(r"^not ok\s+\d+\s*-?\s*(.*)$")
    _TAP_DIRECTIVE = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

    @classmethod
    def _parse_tap(
        cls, log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> bool:
        found = False

        for line in log.splitlines():
            line = line.rstrip()

            # `not ok` must be tested first: `_TAP_OK` would not match it, but
            # keeping the order explicit guards against future pattern edits.
            m = cls._TAP_NOT_OK.match(line)
            if m:
                name = m.group(1)
                directive = cls._TAP_DIRECTIVE.search(name)
                name = cls._TAP_DIRECTIVE.sub("", name).strip()
                if not name:
                    continue
                found = True
                # A `not ok ... # TODO` is an expected failure, not a real one.
                if directive and directive.group(1).upper() == "TODO":
                    skipped.add(name)
                else:
                    failed.add(name)
                continue

            m = cls._TAP_OK.match(line)
            if m:
                name = m.group(1)
                directive = cls._TAP_DIRECTIVE.search(name)
                name = cls._TAP_DIRECTIVE.sub("", name).strip()
                if not name:
                    continue
                found = True
                if directive and directive.group(1).upper() == "SKIP":
                    skipped.add(name)
                else:
                    passed.add(name)

        return found

    @staticmethod
    def _parse_console(
        log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> None:
        """AVA's default reporter: `✔ title`, `✘ title`, `- title`."""
        pass_re = re.compile(r"^\s*[✔✓]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
        fail_re = re.compile(r"^\s*[✘✕✗×]\s+(.+?)$")
        skip_re = re.compile(r"^\s*[-–]\s+(.+?)\s*$")

        for line in log.splitlines():
            line = line.rstrip()
            m = fail_re.match(line)
            if m:
                failed.add(m.group(1).strip())
                continue
            m = pass_re.match(line)
            if m:
                passed.add(m.group(1).strip())
                continue
            m = skip_re.match(line)
            if m:
                skipped.add(m.group(1).strip())
