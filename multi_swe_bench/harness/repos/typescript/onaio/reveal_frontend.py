"""onaio/reveal-frontend harness config — Create React App + Jest + Yarn.

The repo is a CRA app pinned to react-scripts 2.1.3 with jest forced to 25.3.0
through a `resolutions` override. Two consequences drive this config:

* `react-scripts test` starts an interactive watcher unless ``CI`` is set, so
  every stage exports ``CI=true`` or the container would hang until timeout.
* The jest override trips CRA's preflight version check, so ``SKIP_PREFLIGHT_CHECK``
  must be set — upstream CircleCI does exactly the same.

Node is pinned to 12 to match the repo's own CI image (circleci/node:12.16.1).
"""

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Markers wrapping the machine-readable jest report. Parsing JSON rather than
# scraping console output keeps test identities stable across the run/test/fix
# stages, which is what fail-to-pass detection depends on.
JSON_START = "###JEST_JSON_START###"
JSON_END = "###JEST_JSON_END###"
JSON_PATH = "/home/jest-results.json"

# Shared by run.sh / test-run.sh / fix-run.sh. The three stages MUST invoke the
# runner identically — differing flags make their results incomparable and
# silently break fail-to-pass detection.
TEST_ENV = """\
export CI=true
export SKIP_PREFLIGHT_CHECK=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096

# CRA loads REACT_APP_* from .env. The sample holds the defaults the tests
# expect, and the fix patch adds new keys to it, so this must run after the
# patches are applied.
cp -f .env.sample .env 2>/dev/null || true
"""

RUN_TESTS = """\
yarn test --runInBand --verbose --forceExit --json --outputFile=[[JSON_PATH]] 2>&1 || true

echo "[[JSON_START]]"
cat [[JSON_PATH]] 2>/dev/null || true
echo ""
echo "[[JSON_END]]"
"""


def _script(body: str, repo: str, base_sha: str = "") -> str:
    """Fill the placeholders. Plain replacement, not str.format, so the shell's
    own ``${...}`` and ``$(...)`` need no brace escaping."""
    # The block placeholders must expand first: they carry placeholders of their
    # own, which would otherwise survive into the emitted script.
    return (
        body.replace("[[TEST_ENV]]", TEST_ENV)
        .replace("[[RUN_TESTS]]", RUN_TESTS)
        .replace("[[REPO]]", repo)
        .replace("[[BASE_SHA]]", base_sha)
        .replace("[[JSON_PATH]]", JSON_PATH)
        .replace("[[JSON_START]]", JSON_START)
        .replace("[[JSON_END]]", JSON_END)
    )


class RevealFrontendImageBase(Image):
    """Base image: node:12 with the repo cloned at the PR's base commit.

    The official node images are buildpack-deps derived and already ship git,
    so no apt-get is needed here — which is deliberate. node:12 is Debian
    buster, whose apt repositories have been archived, so an `apt-get update`
    would fail.
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

        # DockerfileEnhancer rewrites the clone/COPY line above into a
        # standardized ${REPO_URL} clone + ${BASE_COMMIT} checkout and appends
        # the hardening block, which strips the origin remote and every ref
        # except the detached HEAD. Nothing downstream may assume a remote.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class RevealFrontendImageDefault(Image):
    """PR layer: patches, prepare and the three stage scripts."""

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
        return RevealFrontendImageBase(self.pr, self.config)

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
export SKIP_PREFLIGHT_CHECK=true

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

[[TEST_ENV]]
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

[[TEST_ENV]]
# The test patch can touch package.json, so refresh dependencies.
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

# The fix patch bumps package.json and extends .env.sample, so .env is
# regenerated (inside TEST_ENV) and dependencies reinstalled after patching.
[[TEST_ENV]]
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


@Instance.register("onaio", "reveal-frontend")
class OnaioRevealFrontend(Instance):
    """Harness instance for onaio/reveal-frontend — CRA + Jest."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RevealFrontendImageDefault(self.pr, self._config)

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
        """Prefer the JSON report; fall back to scraping jest's console output.

        The fallback matters because `react-scripts` owns the argv it forwards
        to jest, so `--json`/`--outputFile` are not guaranteed to survive on
        every react-scripts version.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        if not self._parse_json(log, passed, failed, skipped):
            self._parse_console(log, passed, failed, skipped)

        # A name may be reported more than once (retries, duplicate titles).
        # Failure wins over pass, and both win over skip, so the three sets stay
        # disjoint — TestResult raises if they overlap.
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
    def _parse_json(
        log: str, passed: set[str], failed: set[str], skipped: set[str]
    ) -> bool:
        start = log.find(JSON_START)
        end = log.find(JSON_END)
        if start == -1 or end == -1 or end <= start:
            return False

        blob = log[start + len(JSON_START) : end].strip()
        if not blob:
            return False

        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            return False

        found = False
        for suite in data.get("testResults", []):
            assertions = suite.get("assertionResults", []) or []

            # A suite that failed to compile reports no assertions at all. Record
            # the file itself so the failure is visible instead of vanishing.
            if not assertions:
                message = (suite.get("message") or "").strip()
                status = suite.get("status", "")
                if status == "failed" or message:
                    name = suite.get("name", "").strip()
                    if name:
                        failed.add(name)
                        found = True
                continue

            for assertion in assertions:
                name = (assertion.get("fullName") or "").strip()
                if not name:
                    title = (assertion.get("title") or "").strip()
                    ancestors = assertion.get("ancestorTitles", []) or []
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
