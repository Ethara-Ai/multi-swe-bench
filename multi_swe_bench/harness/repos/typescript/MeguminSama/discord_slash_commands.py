import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
        # jest 26 / TypeScript 4.1 era (mid-2021). node:16 ships git + yarn 1.22,
        # so the image needs no apt-get -- bullseye is EOL and its apt mirrors
        # 404, which would break the build.
        return "node:16"

    def image_tag(self) -> str:
        # PR-scoped, not a bare "base": this image bakes in `git checkout
        # ${BASE_COMMIT}` plus a history scrub that repacks the object store down
        # to that commit's ancestry. Image de-duplication keys on
        # image_name() + ":" + image_tag(), and image_name() is org/repo, so a
        # shared tag would collapse every PR of this repo onto ONE image and the
        # next PR's checkout would die with "reference is not a tree".
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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

# git and curl are already provided by the node:16 base image (buildpack-deps
# derived), so no package installation step is needed here.
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/[[REPO_NAME]]
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
git clean -fd
bash /home/check_git_changes.sh

# yarn.lock is committed at this sha; --frozen-lockfile keeps the dependency
# graph byte-identical across the three phases. `|| true` so a non-fatal
# postinstall/native-build hiccup cannot abort the image build.
yarn install --frozen-lockfile || true

# ...but a *totally* failed install must still fail loudly here rather than
# surfacing later as "0 tests in all three phases", which Report.check() would
# reject as an invalid instance with no usable diagnostic.
test -x node_modules/.bin/jest || {{
  echo "prepare: yarn install did not produce node_modules/.bin/jest" >&2
  exit 1
}}
""".replace("[[REPO_NAME]]", repo_name).format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]
bash /home/build-and-test.sh
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch
bash /home/build-and-test.sh
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/build-and-test.sh
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "build-and-test.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/[[REPO_NAME]]
export CI=true

# package.json `main` is dist/src/index.js and every test does `require("..")`,
# so the TypeScript sources MUST be compiled before jest runs.
#
# `rm -rf dist` is load-bearing, not hygiene: the fix patch deletes
# src/builders.ts and re-creates it as the directory src/builders/. tsc never
# removes stale output, so a leftover dist/src/builders.js would keep winning
# Node's file-before-directory resolution over dist/src/builders/index.js and
# the fix would silently look like a no-op.
rm -rf dist
npx tsc --build tsconfig.json 1>&2

# tests/createDelete*.test.js drive the live Discord API and need
# TESTS_APPID/TESTS_TOKEN/TESTS_PUBKEY secrets we do not have; they are
# excluded so all three phases stay hermetic and deterministic. The remaining
# builder tests are pure and need no network.
npx jest --json --forceExit \\
  --testPathIgnorePatterns="/node_modules/|/tests/createDelete" 2>/dev/null
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        name = self.dependency().image_name()
        tag = self.dependency().image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("MeguminSama", "discord-slash-commands")
class DiscordSlashCommands(Instance):
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

    def _rel_test_path(self, suite_path: str) -> str:
        """Container-absolute suite path -> repo-relative path.

        jest reports `testResults[].name` as an absolute path inside the
        container (`/home/discord-slash-commands/tests/foo.test.js`). Test IDs
        are emitted as `<repo-relative path>::<test id>`, matching the pytest
        node-id shape used across the rest of the dataset -- which is also what
        `report._test_name_matches_files` splits on to tie a credited test back
        to the file that defines it (the reward-hacking guard).
        """
        path = (suite_path or "").replace("\\", "/")
        prefix = f"/home/{self.pr.repo}/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        # Fall back to stripping any /home/<checkout>/ prefix so a relocated
        # workdir still yields a relative path rather than an absolute one.
        match = re.match(r"^/home/[^/]+/(.+)$", path)
        if match:
            return match.group(1)
        return path.lstrip("/")

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        data = None
        try:
            # strict=False: jest embeds literal control characters (newlines from
            # failure messages) inside JSON string values.
            data = json.loads(log, strict=False)
        except (json.JSONDecodeError, ValueError):
            # The report object is preceded by tsc diagnostics / npx noise, so a
            # naive find("{") would latch onto a JS object literal in a stack
            # trace. Anchor on jest's own top-level keys instead.
            start = -1
            for marker in (
                '{"numFailedTestSuites"',
                '{"numTotalTestSuites"',
                '{"numTotalTests"',
                '{"testResults"',
            ):
                pos = log.find(marker)
                if pos != -1 and (start == -1 or pos < start):
                    start = pos
            if start != -1:
                end = log.rfind("}")
                if end > start:
                    try:
                        data = json.loads(log[start : end + 1], strict=False)
                    except (json.JSONDecodeError, ValueError):
                        pass

        if data is None:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        # Pass 1: collect (file, describe chain, test name, status). Names are
        # resolved only afterwards, so a collision is detected before it can
        # silently merge two distinct tests onto one id.
        entries: list[tuple[str, str, str, str]] = []
        for suite in data.get("testResults", []):
            suite_path = self._rel_test_path(suite.get("name") or "")
            for test in suite.get("assertionResults", []):
                ancestors = [a for a in (test.get("ancestorTitles") or []) if a]
                title = (test.get("title") or "").strip()
                if not title:
                    # jest omits `title` only on malformed results; fullName is
                    # "<describe...> <title>", so strip the describe prefix off.
                    full = (test.get("fullName") or "").strip()
                    described = " ".join(ancestors)
                    title = (
                        full[len(described) :].strip()
                        if described and full.startswith(described)
                        else full
                    )
                if not title:
                    continue
                entries.append(
                    (suite_path, " > ".join(ancestors), title, test.get("status", ""))
                )

        # Ids are `<file>::<test name>` -- the pytest node-id shape, where the
        # part after "::" is the test itself. jest allows the same test name in
        # two describe blocks of one file, which pytest cannot produce; for that
        # (only) case the describe chain is re-attached, because collapsing both
        # onto one id would drop a test or put it in two buckets at once.
        describes_per_test: dict[tuple[str, str], set[str]] = {}
        for path, describe, title, _ in entries:
            describes_per_test.setdefault((path, title), set()).add(describe)
        ambiguous = {key for key, seen in describes_per_test.items() if len(seen) > 1}

        for path, describe, title, status in entries:
            name = (
                f"{describe} > {title}"
                if describe and (path, title) in ambiguous
                else title
            )
            if path:
                name = f"{path}::{name}"

            if status == "passed":
                passed_tests.add(name)
            elif status == "failed":
                failed_tests.add(name)
            elif status in ("skipped", "pending", "todo", "disabled"):
                skipped_tests.add(name)

        # A suite that fails to even load reports no assertions; surface it so the
        # phase does not silently look like "0 tests, nothing wrong".
        for suite in data.get("testResults", []):
            if suite.get("assertionResults"):
                continue
            if suite.get("status") == "failed" or suite.get("message"):
                failed_tests.add(
                    self._rel_test_path(suite.get("name") or "") or "unknown test suite"
                )

        # TestResult rejects overlapping buckets.
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
