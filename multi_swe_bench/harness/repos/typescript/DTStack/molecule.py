"""DTStack/molecule harness config — Jest 26 + babel-jest, Yarn 1, React/TypeScript.

PR #641 adds a helper for reading the current colour theme's mode. It is a
small, well-scoped change: the test patch adds
``src/common/__tests__/utils.test.ts`` and
``src/services/__tests__/colorThemeService.test.ts``, and the fix patch touches
three source files. No binary hunks, and the fix patch does not touch the tests
that grade it, so report.py's reward-hacking guard is not engaged.

Pinned from the repo at the PR's base commit:

* node 14           — nothing pins a version (no ``engines``, no ``.nvmrc``, and
                     ``.github/workflows/main.yml`` does not set node-version),
                     so this tracks the toolchain's era: jest 26, TypeScript 4.0,
                     sass 1.26, @testing-library/react 11.
* jest ^26.0.1      configured by ``jest.config.js``: testMatch covers
                     ``**/__tests__/**`` and ``**/test/**``, setupFiles are
                     ``jest-canvas-mock`` and ``./test/setupTests.tsx``, and
                     moduleNameMapper stubs media, styles and monaco.
* yarn 1            ``yarn.lock`` is the only lockfile.

Why yarn, specifically
----------------------
``react`` and ``react-dom`` are declared **only** as peerDependencies (^16.14.0)
-- they appear in neither ``dependencies`` nor ``devDependencies``. Yarn 1 does
not install peers, so React is only present because a devDependency (Storybook)
pulls it in transitively, exactly as resolved in ``yarn.lock``. Installing with
npm would re-resolve that tree and can leave React missing, which breaks
``test/setupTests.tsx`` and every rendering test. ``--frozen-lockfile`` keeps
the resolution identical to CI's.

No system packages are needed: ``sass`` is dart-sass (pure JS), ``jest-canvas-mock``
is a pure-JS canvas stub, and there is no node-sass, node-canvas, puppeteer or
electron anywhere in the tree. That is also why this image never runs apt-get --
``node:14`` is Debian buster, whose repositories are archived, so an
``apt-get update`` would fail. The stock image already provides git.

Type-checking is not part of the test path: ``jest.config.js`` declares no
``transform``, so jest 26 falls back to babel-jest with ``babel.config.js`` and
``@babel/preset-typescript``, which strips types rather than checking them.
``yarn check-types`` (tsc) is a separate CI step and is deliberately not run
here -- a type error elsewhere in the repo must not zero out this stage.
"""

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

JSON_START = "###JEST_JSON_START###"
JSON_END = "###JEST_JSON_END###"
JSON_PATH = "/home/jest-results.json"

# Shared by run.sh / test-run.sh / fix-run.sh. The three stages MUST invoke the
# runner identically -- any divergence makes their results incomparable and
# silently breaks fail-to-pass detection.
#
# `--coverage` is dropped from the package.json test script: jest.config.js sets
# no coverage thresholds, so it only costs time and noise here.
RUN_TESTS = """\
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

npx --no-install jest --no-cache --ci \\
    --json --outputFile=[[JSON_PATH]] 2>&1 || true

echo "[[JSON_START]]"
cat [[JSON_PATH]] 2>/dev/null || true
echo ""
echo "[[JSON_END]]"
"""


def _script(body: str, repo: str, base_sha: str = "") -> str:
    """Fill the placeholders.

    Plain replacement rather than str.format, so the shell's own ``${...}`` and
    ``$(...)`` need no brace escaping. The block placeholder expands first
    because it carries placeholders of its own.
    """
    return (
        body.replace("[[RUN_TESTS]]", RUN_TESTS)
        .replace("[[REPO]]", repo)
        .replace("[[BASE_SHA]]", base_sha)
        .replace("[[JSON_PATH]]", JSON_PATH)
        .replace("[[JSON_START]]", JSON_START)
        .replace("[[JSON_END]]", JSON_END)
    )


class MoleculeImageBase(Image):
    """Base image: node 14 with the repo cloned at the PR's base commit.

    No apt-get, deliberately -- see the module docstring. The stock node image
    is buildpack-deps derived and already ships git.
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
        return "node:14"

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


class MoleculeImageDefault(Image):
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
        return MoleculeImageBase(self.pr, self.config)

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

# yarn, not npm: React is a peerDependency only and reaches node_modules
# transitively via the resolution recorded in yarn.lock. See the module
# docstring. build/preinstall.js runs here and only asserts node >= 10.
yarn install --frozen-lockfile --network-timeout 600000 \\
    || yarn install --network-timeout 600000
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
export CI=true
yarn install --network-timeout 600000 || true

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
export CI=true
yarn install --network-timeout 600000 || true

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


@Instance.register("DTStack", "molecule")
class DTStackMolecule(Instance):
    """Harness instance for DTStack/molecule — Jest 26 via babel-jest."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MoleculeImageDefault(self.pr, self._config)

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
        """Prefer jest's JSON report; fall back to scraping console output."""
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        if not self._parse_json(log, passed, failed, skipped):
            self._parse_console(log, passed, failed, skipped)

        # A name may be reported more than once (retries, duplicate titles).
        # Failure wins over pass, and both win over skip, so the three sets stay
        # disjoint -- TestResult raises if they overlap.
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

    @staticmethod
    def _extract_json(log: str) -> Optional[dict]:
        start = log.find(JSON_START)
        end = log.find(JSON_END)
        if start == -1 or end == -1 or end <= start:
            return None

        blob = log[start + len(JSON_START) : end].strip()
        if not blob:
            return None

        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass

        # Node warnings can land on the same stream; fall back to the outermost
        # {...} span.
        first, last = blob.find("{"), blob.rfind("}")
        if first == -1 or last <= first:
            return None
        try:
            return json.loads(blob[first : last + 1])
        except json.JSONDecodeError:
            return None

    @classmethod
    def _parse_json(
        cls, log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> bool:
        data = cls._extract_json(log)
        if not isinstance(data, dict):
            return False

        found = False
        for suite in data.get("testResults", []) or []:
            if not isinstance(suite, dict):
                continue
            assertions = suite.get("assertionResults") or []

            # A suite that failed to compile reports no assertions at all.
            # Record the file itself so the failure is visible rather than
            # silently vanishing from the stage.
            if not assertions:
                message = (suite.get("message") or "").strip()
                if suite.get("status") == "failed" or message:
                    name = (suite.get("name") or "").strip()
                    if name:
                        failed.add(name)
                        found = True
                continue

            for assertion in assertions:
                if not isinstance(assertion, dict):
                    continue
                name = (assertion.get("fullName") or "").strip()
                if not name:
                    title = (assertion.get("title") or "").strip()
                    ancestors = assertion.get("ancestorTitles") or []
                    name = " ".join([a for a in ancestors if a] + [title]).strip()
                if not name:
                    continue

                found = True
                status = assertion.get("status", "")
                if status == "passed":
                    passed.add(name)
                elif status == "failed":
                    failed.add(name)
                elif status in ("skipped", "pending", "todo", "disabled"):
                    skipped.add(name)

        return found

    @staticmethod
    def _parse_console(
        log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> None:
        passed_res = [
            re.compile(r"^PASS:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"),
            re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"),
        ]
        failed_res = [
            re.compile(r"^FAIL:?\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"),
            re.compile(r"^\s*[✕×✗]\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"),
        ]
        skipped_res = [
            re.compile(r"^\s*[○✎]\s+(?:skipped|todo)\s+(.+?)$"),
        ]

        for line in log.splitlines():
            line = line.rstrip()
            for rx in failed_res:
                m = rx.match(line)
                if m:
                    failed.add(m.group(1).strip())
            for rx in passed_res:
                m = rx.match(line)
                if m:
                    passed.add(m.group(1).strip())
            for rx in skipped_res:
                m = rx.match(line)
                if m:
                    skipped.add(m.group(1).strip())
