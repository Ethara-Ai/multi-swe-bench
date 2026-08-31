import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------
# Patch re-split.
#
# The collector classifies a diff hunk as "test" by substring-matching "test"
# against the whole path (collect/build_dataset.py:81). This repo is an ESLint
# plugin *about a test runner*, so many of its production rule files are named
# with "test" in them -- rules/no-only-test.js, rules/no-skip-test.js,
# rules/no-todo-test.js, rules/test-title.js. Those get misfiled into
# test_patch, which carries the real fix into the test stage: the tests then
# pass before the fix patch is ever applied and the f2p signal is destroyed.
# pr-325 is exactly this -- its fix_patch held only readme.md while
# rules/no-only-test.js and rules/no-skip-test.js sat in test_patch, so all
# three stages read 1022/0/0 and the instance was rejected.
#
# Re-split here, in this repo's own config, by path *structure* instead of
# substring. Fixing the collector would be the general cure, but that is shared
# code affecting every language; this keeps the correction scoped to the repo
# that needs it.
# --------------------------------------------------------------------------

_TEST_DIRS = {
    "test",
    "tests",
    "e2e",
    "testing",
    "__tests__",
    "spec",
    "specs",
    "integration-test",
    "integration-tests",
}

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")


def _is_test_path(path: str) -> bool:
    """True when the path is genuinely a test file.

    Matches on directory components and on the *standard* test-file naming
    conventions only. Deliberately does NOT treat a trailing "-test"/"-spec" as
    a test marker: a hyphen is an ordinary word separator in source filenames,
    which is precisely what misfiles rules/no-only-test.js.
    """
    if any(part.lower() in _TEST_DIRS for part in path.split("/")[:-1]):
        return True

    base = posixpath.basename(path).lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return (
        stem in ("test", "spec", "conftest")
        or re.match(r"^(test_|spec_)", stem) is not None
        or re.search(r"(_test|_spec)$", stem) is not None  # foo_test.go
        or re.search(r"\.(test|spec)$", stem) is not None  # foo.test.js
    )


def _split_file_diffs(patch: str) -> Optional[list[tuple[str, str]]]:
    """Split a unified diff into [(path, text), ...].

    Returns None if the text does not parse cleanly as a sequence of
    `diff --git` blocks, so the caller can fall back to the original patches
    rather than risk emitting a corrupted one.
    """
    if not patch or not patch.strip():
        return []
    if not patch.lstrip().startswith("diff --git "):
        return None

    blocks: list[tuple[str, str]] = []
    path: Optional[str] = None
    buf: list[str] = []
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if path is not None:
                blocks.append((path, "".join(buf)))
            match = _DIFF_HEADER.match(line.rstrip("\n"))
            if match is None:
                return None
            path = match.group(2)
            buf = [line]
        else:
            if path is None:
                return None
            buf.append(line)
    if path is not None:
        blocks.append((path, "".join(buf)))
    return blocks


def resplit_patches(fix_patch: str, fix_test_patch: str) -> tuple[str, str]:
    """Return (code-only patch, test-only patch) re-derived from both halves.

    Falls back to the inputs unchanged if either side fails to parse, or if the
    re-split would leave either half empty (the resolve step rejects an empty
    patch, so a bad split must never be allowed to ship).
    """
    left = _split_file_diffs(fix_patch)
    right = _split_file_diffs(fix_test_patch)
    if left is None or right is None:
        return fix_patch, fix_test_patch

    blocks = left + right
    code = "".join(text for path, text in blocks if not _is_test_path(path))
    tests = "".join(text for path, text in blocks if _is_test_path(path))

    # Never emit an empty half, and never silently drop a hunk.
    if not code.strip() or not tests.strip():
        return fix_patch, fix_test_patch
    if len(code) + len(tests) != sum(len(text) for _, text in blocks):
        return fix_patch, fix_test_patch
    return code, tests


class EslintPluginAvaImageBase(Image):
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
        return "node:20-bookworm"

    # One shared base image for the whole PR range (#277-#325), per the
    # base-pr-<low>-to-<high> convention. NOTE: the enhancer prunes the clone to
    # the history reachable from ${BASE_COMMIT}, and the build arg comes from
    # whichever instance wins the image dedup -- the first in dataset order.
    # The dataset must therefore be ordered newest-PR-first, so the base is cut
    # at #325 and every earlier base commit remains reachable as an ancestor.
    def image_tag(self) -> str:
        return "base-pr-277-to-325"

    def workdir(self) -> str:
        return "base-pr-277-to-325"

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

{code}
{self.clear_env}
"""


class EslintPluginAvaImageDefault(Image):
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
        return EslintPluginAvaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # Correct the collector's substring-based split before staging the
        # patches (see resplit_patches above). For PRs the collector got right
        # this is a no-op; for pr-325 it moves rules/no-*-test.js out of the
        # test patch and into the fix patch, which is what restores the f2p
        # signal.
        fix_patch, test_patch = resplit_patches(
            self.pr.fix_patch, self.pr.test_patch
        )

        return [
            File(
                ".",
                "fix.patch",
                fix_patch,
            ),
            File(
                ".",
                "test.patch",
                test_patch,
            ),
            # Integrity guard: asserts we are in a git repo and the tree is clean.
            # prepare.sh calls it on both sides of the checkout so a stray edit can
            # never reach the graded runs unnoticed.
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
set -e

cd /home/{pr.repo}

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "ERROR: /home/{pr.repo} is not a git repository" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "check_git_changes: clean tree at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

# Toolchain top-up. node:20-bookworm already ships git 2.39.5 and
# ca-certificates -- verified in the image -- so the base Dockerfile no longer
# apt-installs anything and this is a safety net, not a dependency. `|| true`
# because a transient apt failure must not fail the build when the packages are
# already present; if git were genuinely missing the git commands below would
# fail loudly under `set -e` anyway.
apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/* || true

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The shared base image is cut at the NEWEST commit in the era range, so rewinding
# HEAD to this PR's base commit strands the newer commits in the object store as
# unreachable-but-recoverable objects -- `git fsck --lost-found` and
# `git cat-file --batch-all-objects` list them without needing to know a SHA, which
# would hand a solving agent the future fix commits. Re-prune here so the container
# ships only this PR's own history, then assert it: every commit object left in the
# store must be reachable from HEAD. (The base's own assert compares
# `rev-list --all` to `rev-list HEAD`, and neither counts unreachable objects, so it
# cannot catch this.)
git reflog expire --expire=now --all
git gc --prune=now --quiet
test "$(git cat-file --batch-all-objects --batch-check='%(objecttype)' \\
    | grep -c '^commit$')" = "$(git rev-list HEAD --count)"

rm -rf node_modules
npm install --force || true
""".format(pr=self.pr),
            ),
            # `npm test` is `xo && nyc ava`; the lint pass is unrelated to the
            # oracle and would abort the run before any TAP is emitted, so AVA
            # is invoked directly. --tap because the default mini reporter is
            # not machine-readable, --concurrency=1 to keep the execution order
            # stable across the three stages.
            #
            # All three stages run the same install and the same test command.
            # Several PRs add a runtime dependency in the same fix_patch that
            # uses it (micro-spelling-correcter in pr-277, eslint-utils in
            # pr-307), so the install is required after patching -- and it has
            # to run unpatched too, or stage 1 resolves a different dependency
            # tree than stages 2 and 3 and the comparison is unsound.
            #
            # No `|| true` on these installs (unlike prepare.sh): this repo has
            # no native addons, so a failing install at run time is an
            # environment fault, not an expected arm64 compile failure. Masking
            # it would let AVA error on a missing module and report every test
            # as failed, turning a working fix into a zero-f2p instance.
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npm install --force
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch
npm install --force
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn --binary /home/test.patch /home/fix.patch
npm install --force
npx ava --tap --concurrency=1 2>&1
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        # Derive the COPY lines from files() so the two can never drift apart --
        # a file added to files() but not COPY'd (or vice versa) is a build failure
        # that only shows up at runtime.
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

{copy_commands}RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("avajs", "eslint-plugin-ava")
class EslintPluginAva(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EslintPluginAvaImageDefault(self.pr, self._config)

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

        # Strip ANSI before matching -- npm/AVA still colour some output even
        # under the TAP reporter, and the escapes break the anchored patterns.
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # AVA TAP output: a `# <file or title>` comment introduces a block, then
        # one `ok N <title>` / `not ok N <title>` line per test. `1..N` closes
        # the stream and is followed by a summary block whose `# ` lines must
        # not be mistaken for test names.
        #
        # The TAP sequence number is matched but deliberately NOT kept in the
        # name. test.patch adds tests, which shifts the ordinal of every test
        # after the insertion point, so a name containing it would differ
        # between the run/test/fix stages. Report.__post_init__ unions names
        # across the three stages, so the same test would split into two
        # entries with TestStatus.NONE filling the gaps -- exactly the
        # PASS/NONE/FAIL anomaly that Report.check() rule 4 rejects.
        test_pattern = re.compile(r"^(ok|not ok) \d+ (?:- )?(.*?)(?: #.*)?$")
        tap_started = False
        current_block = None

        for line in log.split("\n"):
            line = line.strip()

            if line.startswith("TAP version 13"):
                tap_started = True
                continue

            if not tap_started:
                continue

            if line.startswith("1.."):
                break

            if line.startswith("# "):
                current_block = line[2:].strip()

            elif line.startswith(("ok", "not ok")):
                match = test_pattern.match(line)
                if not match:
                    continue

                status, title = match.groups()
                title = title.strip()

                # AVA emits a `# <title>` comment immediately before each test,
                # so the block header is usually the title itself; only prefix
                # when it carries extra context (e.g. the test file).
                test_name = (
                    f"{current_block}: {title}"
                    if current_block and current_block != title
                    else title
                )

                if "# SKIP" in line or "# TODO" in line:
                    skipped_tests.add(test_name)
                elif status == "ok":
                    passed_tests.add(test_name)
                else:
                    failed_tests.add(test_name)

        # TestResult.__post_init__ requires the three sets to be disjoint and
        # raises ValueError otherwise. Names are no longer made unique by an
        # embedded ordinal, so a retried or repeated title can reach two
        # buckets; failed wins over skipped, and skipped over passed.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
