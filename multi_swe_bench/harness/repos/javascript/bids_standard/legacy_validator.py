import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# .circleci/config.yml at this commit runs `image: node:12.16.1`, and
# bids-validator/package.json declares `engines: {"node": ">=10.11.0"}`. The base
# commit is August 2020, so 12.16.1 is both era-correct and exactly what CI used.
# Pinned to the patch version rather than `node:12` so the image is reproducible.
NODE_IMAGE = "node:12.16.1"

# The suite is scoped to the spec files the gold patch touches. The full suite
# walks the whole bids-examples data set across every validator and takes far
# longer, for signal that has nothing to do with this PR.
TEST_FILES = (
    "bids-validator/tests/bids.spec.js "
    "bids-validator/tests/tsv.spec.js "
    "bids-validator/utils/files/__tests__/readDir-examples.spec.js"
)

# --ci keeps jest from writing new snapshots (it fails instead, which is what a
# graded run wants). --runInBand avoids the worker pool: these specs read the
# same on-disk example data, and parallel workers make ordering-dependent
# failures that have nothing to do with the patch.
TEST_CMD = f"npx jest --ci --runInBand --verbose {TEST_FILES}"

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR). The harness streams `docker buildx` output through
# subprocess with `text=True` and no explicit encoding, so a Windows host
# decodes it as cp1252 and any UTF-8 byte outside that map aborts the build with
# "'charmap' codec can't decode byte 0x81". yarn and jest both draw progress
# with box characters, so this is not hypothetical.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# The gold test patch does not only add assertions - it also bumps the
# `bids-examples` SUBMODULE pointer, because the new tests need example datasets
# that only exist in the newer revision:
#
#     -Subproject commit ac55ad3eb2d7aa40b4ecd05c316b2b2d7eeecffb
#     +Subproject commit 32eecf4a4cea6dff5ca9e31bb176f29503b66d7d
#
# `git apply` updates that gitlink in the index but does NOT check out the new
# content, so without an explicit `git submodule update` after every apply the
# tests would run against the OLD data and fail for a reason that has nothing to
# do with the fix. Both revisions are fetched at build time so the graded stages
# need no network.
_SUBMODULE_PATH = "bids-validator/tests/data/bids-examples"


def _submodule_shas(test_patch: str) -> tuple[str, str]:
    """(base, target) commits of the bids-examples submodule, read from the patch."""
    before = re.search(r"^-Subproject commit ([0-9a-f]{40})", test_patch or "", re.M)
    after = re.search(r"^\+Subproject commit ([0-9a-f]{40})", test_patch or "", re.M)
    return (before.group(1) if before else "", after.group(1) if after else "")


# --------------------------------------------------------------------------
# Patch split hygiene
# --------------------------------------------------------------------------
# The generated test_patch for this PR does not contain tests only: it also
# carries a hunk against `bids-validator/validators/bids/fullTest.js`, which is
# the validator's main execution path. That hunk wires up the new feature:
#
#     + self.issues = self.issues.concat(
#     +     tsv.validateContRec(files.contRecord, jsonContentsDict))
#
# Two things break because of it.
#
#   1. The "tests only" stage is not tests only. The test stage fails with
#      `TypeError: _tsv.default.validateContRec is not a function` - a crash
#      from half-applied feature code, not tests detecting missing behaviour.
#   2. A solver shown test.patch is handed the module (`tsv`), the function
#      name (`validateContRec`) and the exact call signature for free. The
#      split exists precisely so test.patch is safe to show and fix.patch is
#      the answer; here part of the answer sits in test.patch.
#
# So the two patches are re-split before they are written into the image. The
# rule is structural, not a hardcoded filename, so any other production file
# that leaks into a regenerated test_patch is moved as well.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|__mocks__|spec)/|\.(?:spec|test)\.[jt]sx?$"
)


def _is_test_side(path: str) -> bool:
    """True for files that legitimately belong in test.patch.

    Test sources, fixtures under a tests/ directory, and the bids-examples
    submodule gitlink (test DATA - the test patch bumps it, and the stage
    scripts re-checkout the submodule right after applying, so it must stay on
    the test side or the fix stage would run against the wrong examples).
    """
    return path == _SUBMODULE_PATH or bool(_TEST_PATH_RE.search(path))


def _iter_diff_blocks(patch: str):
    """Yield (path, block_text) for each `diff --git` block, in order."""
    if not patch:
        return
    starts = [m.start() for m in re.finditer(r"^diff --git ", patch, re.M)]
    for i, s in enumerate(starts):
        block = patch[s : starts[i + 1] if i + 1 < len(starts) else len(patch)]
        m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if m:
            yield (m.group(2), block)


def _split_patches(test_patch: str, fix_patch: str) -> tuple[str, str]:
    """Move production-code blocks out of test_patch and into fix_patch.

    Order matters on the way out: fix-run.sh runs `git apply test.patch
    fix.patch`, so a moved block is appended to the END of fix.patch and still
    applies cleanly on top of the test patch. Files already present in
    fix.patch are dropped rather than duplicated, because applying the same
    hunk twice fails.
    """
    if not test_patch:
        return test_patch, fix_patch

    already_in_fix = {p for p, _ in _iter_diff_blocks(fix_patch)}
    keep, moved = [], []
    for path, block in _iter_diff_blocks(test_patch):
        if _is_test_side(path):
            keep.append(block)
        elif path in already_in_fix:
            # fix.patch already edits this file; a duplicate block would make
            # `git apply` fail outright. Dropping it is the safe resolution.
            continue
        else:
            moved.append(block)

    if not moved:
        return test_patch, fix_patch

    new_test = "".join(keep)
    new_fix = (fix_patch or "")
    if new_fix and not new_fix.endswith("\n"):
        new_fix += "\n"
    new_fix += "".join(moved)
    return new_test, new_fix


class ImageBase(Image):
    """Per-PR base: Node toolchain + the repo frozen at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is inside
    it. A single shared `base` tag cannot make that promise - the first PR to
    build it freezes it, and every later PR silently inherits the wrong commit
    while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form. That exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite is what supplies the REPO_URL/BASE_COMMIT clone,
    the checkout, the history-sanitising scrub and the final CMD. Decorate that
    line and the enhancer stops recognising it, so the clone stays raw and the
    entire hardening block is silently never injected.

    Note the submodule is NOT initialised here. A plain clone leaves it empty,
    so the enhancer's `git submodule foreach` scrub walks nothing; initialising
    it is prepare.sh's job, in the per-PR layer.
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
        return NODE_IMAGE

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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # No `ENV DEBIAN_FRONTEND` here - DockerfileEnhancer already sets it
        # (with LANG and TZ) on every base image.
        #
        # There is deliberately NO apt install line, for two reasons:
        #
        #  1. It is not needed. node:12.16.1 already ships everything this build
        #     uses - verified inside the image: git 2.11.0, yarn 1.22.0, node
        #     12.16.1, and /etc/ssl/certs/ca-certificates.crt. The HTTPS clone
        #     and the yarn registry both work on that trust store as shipped.
        #
        #  2. It is not possible. This image is Debian 9 (stretch), which is
        #     end-of-life; its archives have been withdrawn from deb.debian.org,
        #     so `apt-get update` fails with 404s and exits 100, killing the
        #     build. Pointing apt at archive.debian.org would work, but adds a
        #     network dependency and an unpinned package set for no benefit when
        #     the toolchain is already complete.
        #
        # Keeping the era-correct node version matters more than apt access: the
        # PR is from August 2020 and .circleci/config.yml pinned node:12.16.1.
        return f"""FROM {image_name}

{self.global_env}

ENV CI=true \\
    NO_COLOR=1 \\
    npm_config_color=false \\
    npm_config_progress=false

WORKDIR /home/

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        sub_base, sub_target = _submodule_shas(self.pr.test_patch)

        # Re-synchronise the submodule working tree with whatever gitlink the
        # index currently holds. Runs in every stage AFTER the patches are
        # applied, because `git apply` moves the pointer without moving the
        # files. `--no-fetch` keeps the graded stages offline - prepare.sh has
        # already fetched both revisions into the submodule's object store.
        sync_submodule = (
            f"git submodule update --init --recursive --no-fetch {_SUBMODULE_PATH} "
            f"|| git submodule update --init --recursive {_SUBMODULE_PATH}"
        )

        # Keep production code out of test.patch - see _split_patches above.
        test_patch, fix_patch = _split_patches(self.pr.test_patch, self.pr.fix_patch)

        return [
            File(".", "fix.patch", fix_patch),
            File(".", "test.patch", test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Integrity guard. prepare.sh calls this immediately after `git reset --hard`
# and again after `git checkout <BASE_COMMIT>`, so a tree that did not actually
# come back clean aborts the BUILD instead of being baked into the image and
# silently contaminating all three graded stages.
#
# --ignore-submodules is deliberate. This repo carries the bids-examples data
# submodule, and a submodule whose checked-out commit differs from the gitlink
# shows up as a dirty parent tree. That difference is expected and managed
# explicitly by the stage scripts; what this guard is for is stray files in the
# PARENT working tree, so the submodule is excluded to keep the signal honest.
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain --ignore-submodules=all) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain --ignore-submodules=all
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

cd /home/{repo}
git reset --hard
# Assert the reset really produced a clean tree, so a leftover file cannot be
# baked into the image and silently inherited by all three graded stages.
bash /home/check_git_changes.sh

# The base image is frozen at ONE commit and has had its origin remote stripped
# by the hardening block, so a commit that is not already present cannot be
# resolved locally and `git fetch origin` has no remote to use. Ask GitHub for
# that exact commit by sha over the full URL.
FETCHED=0
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
    FETCHED=1
fi
git checkout {sha}
bash /home/check_git_changes.sh

if [ "$FETCHED" = "1" ]; then
    git checkout --detach {sha}
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    git repack -a -d -l --quiet
    rm -f .git/objects/info/alternates
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# The bids-examples submodule holds the example datasets every spec reads, and
# `bids-validator/bin/test-submodule-exists` refuses to run jest without it.
git submodule update --init --recursive {sub_path}

# Fetch BOTH revisions of the data now, at build time. The test patch bumps the
# gitlink from {sub_base} to {sub_target}, so the fix stage
# needs the newer one - and the graded stages must not depend on the network.
( cd {sub_path} \\
  && git fetch --quiet origin {sub_base} 2>/dev/null || true ) || true
( cd {sub_path} \\
  && git fetch --quiet origin {sub_target} 2>/dev/null || true ) || true

# Scrub the submodule, exactly as the base image's hardening block scrubs the
# parent repo. Without this the image ships a SECOND git repository that was
# never sanitised: a live `origin` pointing at GitHub, every branch and tag it
# publishes, and revisions newer than anything under test. The base's
# `git submodule foreach` scrub cannot cover it - the base clone is not
# recursive, so at that point no submodule exists yet; it is created here, in
# this layer, and this is the only place that can clean it.
#
# The two commits the graded stages need are pinned under refs/pinned/ first so
# `gc --prune=now` cannot collect them. That namespace is deliberate: it is
# outside refs/heads, refs/remotes, refs/tags and refs/replace, so the standard
# integrity assertions below still hold, and nothing here re-introduces a
# branch or tag a solver could enumerate.
#
# Honest limitation, recorded rather than hidden: the ancestry of {sub_target}
# survives, because that commit must stay resolvable and its parents are part
# of it. Truncating it would rewrite the sha and break the gitlink the test
# patch pins. What this removes is the live remote and every ref - the parts
# that actually leak, and that the base's D14/D15 machinery exists to kill.
(
    # `set -e` and a bare `cd` on its own line, deliberately. Written as
    # `cd path && cmd || true` the `|| true` swallows a failed cd as well, and
    # every following command would then run against the PARENT repository -
    # deleting its refs instead of the submodule's. Here a failed cd aborts.
    set -e
    cd {sub_path}
    git remote remove origin 2>/dev/null || true

    # Pin both needed commits before gc, or --prune=now collects the one that
    # is not currently checked out.
    for c in {sub_base} {sub_target}; do
        if git cat-file -e "$c" 2>/dev/null; then
            git update-ref "refs/pinned/$c" "$c"
        fi
    done

    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --quiet
    # A submodule's `.git` is a FILE holding a gitdir: pointer, so
    # `.git/objects/...` does not exist here - the real object store lives under
    # the parent's .git/modules/<name>/. Ask git for the path instead of guessing.
    rm -f "$(git rev-parse --git-dir)/objects/info/alternates"
    git config --local gc.auto 0

    test -z "$(git remote)"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    git cat-file -e {sub_base}
    git cat-file -e {sub_target}
)

# Install the dependency tree into the image layer so the three scored stages do
# not re-download it three times over.
yarn install --frozen-lockfile --non-interactive || yarn install --non-interactive || npm ci || npm install

# Warm jest's transform cache; the outcome is irrelevant to grading.
{test_cmd} > /dev/null 2>&1 || true
git reset --hard
git clean -qfd
""".format(
                    repo=self.pr.repo,
                    org=self.pr.org,
                    sha=self.pr.base.sha,
                    sub_path=_SUBMODULE_PATH,
                    sub_base=sub_base,
                    sub_target=sub_target,
                    test_cmd=TEST_CMD,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
{sync}
{test_cmd}
""".format(repo=self.pr.repo, sync=sync_submodule, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{sync}
{test_cmd}
""".format(repo=self.pr.repo, sync=sync_submodule, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{sync}
{test_cmd}
""".format(repo=self.pr.repo, sync=sync_submodule, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

# `-o pipefail` so a failing prepare.sh still fails the build: without it the
# pipeline would report the exit status of `tr`, which always succeeds, and a
# broken image would be published as if it were good.
RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {ASCII_FILTER}"

{self.clear_env}

"""


@Instance.register("bids-standard", "legacy-validator")
class LegacyValidator(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # jest --verbose prints a tree, one line per test:
        #     PASS bids-validator/tests/tsv.spec.js
        #       TSV
        #         v should not allow  (3 ms)
        #         x should catch ...
        # The tick/cross glyphs are stripped by the ASCII filter, so the marker
        # cannot be relied on. What survives and IS reliable is jest's own
        # summary section, which names every failing test explicitly:
        #     * TSV > should catch ...
        # plus the per-file PASS/FAIL banner.
        #
        # So results are taken from the indented test lines, keyed by the file
        # banner that precedes them - that keeps ids unique across spec files and
        # stable across the three stages.
        re_file = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.js)\b")
        # An indented leaf line: two or more spaces, optional status glyph
        # remnants, the name, and an optional "(12 ms)" timing suffix.
        re_leaf = re.compile(r"^\s{4,}(?:[^\w\s]\s+)?(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_todo = re.compile(r"^\s{4,}(?:todo|skipped)\s+(.+?)\s*$", re.I)

        # After the verbose tree, jest prints a detail block for every failure:
        # the assertion message, an excerpt of the source with line numbers, and
        # a stack trace. Those lines are indented too, so `re_leaf` happily
        # matched them and invented test ids like
        #     ...bids.spec.js::211 |       assert(
        #     ...readDir-examples.spec.js::at toHaveLength (....js:9:27)
        # 23 of them reached p2p_tests in the delivered dataset. They are not
        # tests; they are the innards of a failure report. Recognise and skip.
        re_noise = re.compile(
            r"""^(?:
                  \d+\s*\|                 # source excerpt:  211 |   assert(
                | at\s                     # stack frame:     at Object.<anonymous> (...)
                | [-+]\s*\d*\s*\|          # +/- marked source excerpt
                | [\[\]{}"']               # JSON / evidence fragments
                | (?:Expected|Received|Difference|Object|Array|Error|TypeError)\b
                | [-=]{3,}                 # rule lines
                )""",
            re.X,
        )
        # Same thing seen from the other end: a genuine jest test name never
        # contains executable source or a file:line:col reference.
        re_code_like = re.compile(
            r"(?:expect\(|assert\(|\.js:\d+:\d+|=>\s|\}\s*\)|\bfunction\b)"
        )

        def is_noise(name: str) -> bool:
            return bool(re_noise.match(name)) or bool(re_code_like.search(name))

        current_file = ""
        current_status = ""

        def record(name: str, status: str) -> None:
            if status == "pass":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            elif status == "fail":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        # jest's machine-readable summary: every failing test is listed under
        # "Summary of all failing tests" / as a "* name" bullet. Collect those
        # first so a leaf line can never mark a known failure as passing.
        failing_names = set(
            m.group(1).strip()
            for m in re.finditer(r"^\s*(?:✕|x|\*)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", log, re.M)
            if not is_noise(m.group(1).strip())
        )

        for raw_line in log.splitlines():
            line = raw_line.rstrip()

            m = re_file.match(line)
            if m:
                current_status = "fail" if m.group(1) == "FAIL" else "pass"
                current_file = m.group(2)
                continue

            if not current_file or not line.strip():
                continue
            if line.lstrip().startswith(("PASS", "FAIL", "Tests:", "Test Suites:", "Snapshots:", "Time:", "Ran all")):
                continue

            m = re_todo.match(line)
            if m:
                record(f"{current_file}::{m.group(1).strip()}", "skip")
                continue

            m = re_leaf.match(line)
            if m:
                name = m.group(1).strip()
                if not name or name.endswith(":") or is_noise(name):
                    continue
                test_id = f"{current_file}::{name}"
                # A file-level FAIL banner does not mean every leaf failed, so a
                # leaf is only failed when jest itself listed it as failing.
                record(test_id, "fail" if name in failing_names else "pass")

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
