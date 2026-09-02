import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# the-guild-org/apollo-angular -- Apollo Client bindings for Angular.
#
# Dataset analysis (output/the-guild-org__apollo-angular_raw_dataset.jsonl):
#   * 1 instance, PR #2316, base.sha cafb23a797371b2f4df5aae4891cf528cdbcfa58,
#     base.ref `master`, merged 2024-11-04. Single era: the record carries no
#     `number_interval` and no `tag`, so Instance.create() resolves the key
#     `the-guild-org/apollo-angular` (instance.py:44-49).
#   * Yarn **classic** workspace monorepo: `yarn.lock` is "yarn lockfile v1",
#     there is no `.yarnrc.yml` and no `packageManager` field in package.json,
#     so this is yarn 1.x -- the copy bundled with the official node image
#     (1.22.22). Berry-only knobs (`corepack enable`,
#     YARN_ENABLE_IMMUTABLE_INSTALLS, YARN_NODE_LINKER) are deliberately absent
#     because they do not apply to yarn classic.
#   * `.nvmrc` = 18, `engines.node` = ">=16", and .github/workflows/main.yml
#     pins `node-version: 18` for both the Build and the Tests job, so
#     `node:18-bookworm` is the era-correct base image.
#   * Workspaces: `packages/*` and `website`. Only `packages/apollo-angular`
#     has real tests -- `packages/demo` has `"test": "exit 0"` and `website`
#     has `"test": "echo 'nothing to do here'"` -- so the root
#     `yarn test` (= `yarn workspaces run test`) resolves to a single jest run
#     over packages/apollo-angular. That is exactly what CI runs
#     (`yarn test --ci`), so the same command is used here, plus `--verbose` so
#     jest prints one line per test case instead of one line per file.
#
# Why every stage rebuilds the package before testing:
#   Three spec files (testing/tests/integration.spec.ts,
#   testing/tests/module.spec.ts, tests/integration.spec.ts) import the package
#   by its published name (`apollo-angular`, `apollo-angular/testing`). The
#   workspace symlink resolves that to packages/apollo-angular, whose
#   `typings` field points at `build/index.d.ts` -- a file that only exists
#   after `yarn workspace apollo-angular build`. Without the build, ts-jest
#   fails those suites with `TS2307: Cannot find module 'apollo-angular'`
#   (observed: 2 suites failing, 131 passed). CI has the same ordering: the
#   Tests job `needs: build` and restores the build directory from cache.
#   The build runs in all three run scripts, not just prepare.sh, because
#   test.patch edits `testing/src/operation.ts`, which feeds that build output.
#
#   The run-script build is deliberately NOT suffixed with `|| true`. Under
#   `set -eo pipefail` a build failure aborts the stage before jest runs, the
#   stage captures no test output, and `Report.check()` rule 1
#   (`fix_patch_result.all_count == 0`, report.py:205-210) rejects the record
#   loudly. That is the outcome we want: a broken build is an infrastructure
#   failure, not a test result. Swallowing it with `|| true` would instead let a
#   build that breaks in only one stage launder itself into a jest "Test suite
#   failed to run" FAIL -- indistinguishable from a genuine f2p, and silent.
#   Verified non-hypothetical in the reference run: ng-packagr emits all five
#   `Built apollo-angular/*` markers and exits 0 in all three stages, so the
#   `|| true` was never load-bearing. `|| true` survives only in prepare.sh,
#   where QC sanctions it for the install and where the build is a cache
#   warm-up whose failure the run scripts would catch anyway.
#
# DATASET DEFECT, CORRECTED IN THIS CONFIG -- patch reclassification:
#   The collector put the production fix in test_patch instead of fix_patch,
#   almost certainly because its path contains the segment `testing/`:
#     fix_patch  -> .changeset/perfect-buckets-drum.md          (release note only)
#     test_patch -> packages/apollo-angular/testing/src/operation.ts   <-- THE FIX
#                   packages/apollo-angular/testing/tests/operation.spec.ts
#                   website/src/pages/docs/development-and-testing/testing.mdx
#   `testing/src/operation.ts` is production source shipped in the published
#   package, not test code: it adds `TestOperation.complete()` and stops
#   auto-completing subscriptions -- precisely the behaviour the three new
#   cases in testing/tests/operation.spec.ts assert.
#
#   Left as collected, test.patch carries the fix along with the tests, so the
#   new cases already pass at the test stage, nothing transitions !PASS -> PASS
#   between test and fix, and Report.check() rule 3 (report.py:216-226) rejects
#   the record. Observed on the uncorrected split:
#     run = (155, 0, 4)   test = (158, 0, 4)   fix = (158, 0, 4)
#     -> f2p = {}, valid = False, unresolved_ids = ["...:pr-2316"]
#
#   `_normalize_patches()` below moves that one diff section from test_patch
#   into fix_patch **on the PullRequest record itself**, in `__init__`, before
#   anything downstream reads it. The union of the two patches is byte-for-byte
#   the union the collector produced -- only the split point moves -- so
#   fix-run.sh still reconstructs the exact merged PR state, while test-run.sh
#   now reconstructs base + tests only.
#
#   WHY THE RECORD AND NOT JUST files():
#   Staging corrected *files* while leaving `pr.test_patch` / `pr.fix_patch`
#   uncorrected makes the container disagree with every consumer that reads the
#   record, and one of those consumers rejects the instance outright:
#     * report.py:605-606 builds Report from `instance.pr.{test,fix}_patch`, so
#       `test_patch_files` / `fix_patch_files` -- and therefore the rule-5
#       tamper check (report.py:270-272) -- would describe a split that never
#       ran.
#     * run_evaluation.py:778-779 calls
#       `fix_patch_tampers_with_tests(agent_patch, instance.pr.test_patch)`.
#       With the uncorrected record, `testing/src/operation.ts` counts as a
#       *gold test file*; since that file is where `TestOperation` is declared,
#       adding `TestOperation.complete()` there is the only way to satisfy the
#       f2p -- so every correct submission would be rejected unscored as reward
#       hacking and the instance could never be resolved.
#   Normalizing in `__init__` puts the correction on the record that all three
#   `Instance.create()` call sites (build_dataset.py:475, run_evaluation.py:484,
#   report.py:642) hand to this class, so image, report and grader agree.
#
#   REMAINING GAP (not fixable from this file):
#   `gen_report.run_dataset()` exports rows via
#   `Dataset.build(self.raw_dataset[report.id], report)` (gen_report.py:599),
#   and `self.raw_dataset` is loaded straight from the JSONL by
#   `PullRequest.from_json()` -- it never passes through `Instance.create()`.
#   The exported `Dataset.test_patch` (dataset.py:73) therefore still carries
#   the collector's split, i.e. still contains the production fix. Closing that
#   requires moving the `packages/apollo-angular/testing/src/operation.ts`
#   section from `test_patch` to `fix_patch` in the raw JSONL; once that is
#   done, `_normalize_patches()` becomes a verified no-op (it is idempotent)
#   and can be deleted.

_NODE_IMAGE = "node:18-bookworm"

# --- patch reclassification -------------------------------------------------
#
# Same precedent as the `_strip_binary_diffs` helpers in
# repos/golang/permify/permify.py:9 and repos/golang/traefik/traefik.py:9:
# a pure, module-level transform of the diff text. Unlike those, it is applied
# to the PullRequest record in `__init__` rather than to the staged files, so
# that the image, the Report and the evaluation grader all read one split --
# see "WHY THE RECORD AND NOT JUST files()" above.

# Production-source paths the collector misfiled into test_patch.
_MISFILED_SOURCE_PATHS = frozenset(
    {"packages/apollo-angular/testing/src/operation.ts"}
)

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+)")
_DIFF_BOUNDARY = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _diff_sections(patch: str) -> list[str]:
    """Split a unified diff into one section per file, preserving text exactly.

    Splitting on a zero-width lookahead keeps each `diff --git` header attached
    to its own hunks, so "".join(_diff_sections(p)) == p for any patch that
    starts at a diff header (both patches on this record do).
    """
    if not patch:
        return []
    return [s for s in _DIFF_BOUNDARY.split(patch) if s.strip()]


def _is_misfiled_source(section: str) -> bool:
    header = _DIFF_HEADER.match(section)
    return header is not None and header.group(1) in _MISFILED_SOURCE_PATHS


def _test_patch(pr: PullRequest) -> str:
    """test_patch minus the misfiled production-source sections."""
    return "".join(
        s for s in _diff_sections(pr.test_patch) if not _is_misfiled_source(s)
    )


def _fix_patch(pr: PullRequest) -> str:
    """fix_patch plus the misfiled production-source sections.

    Appended after the record's own fix_patch sections; the two touch disjoint
    files, so `git apply` order is irrelevant and the result stays atomic.
    """
    moved = [s for s in _diff_sections(pr.test_patch) if _is_misfiled_source(s)]
    return "".join(_diff_sections(pr.fix_patch) + moved)


def _normalize_patches(pr: PullRequest) -> PullRequest:
    """Move the misfiled production-source section onto the record, in place.

    Called from every `__init__` that receives a PullRequest, so the correction
    is applied before `files()`, `Report` (report.py:605-606) or the evaluation
    reward-hacking guard (run_evaluation.py:778-779) ever read the patches.

    Idempotent by construction: both new values are computed from the current
    record *before* either is written back, and `_fix_patch()` sources the moved
    section from `pr.test_patch`. Once the move has happened `pr.test_patch` no
    longer contains it, so a second call re-derives exactly the same pair. That
    matters because the same PullRequest may be handed to both the Instance and
    its ImageDefault, and because a corrected JSONL would make this a no-op.

    Safe on the stub record `ReportTask.instance` builds (report.py:621-634),
    which carries empty patches: `_diff_sections("")` is `[]`, so both fields are
    rewritten from `""` to `""`.
    """
    new_test = _test_patch(pr)
    new_fix = _fix_patch(pr)
    pr.test_patch = new_test
    pr.fix_patch = new_fix
    return pr


class ApolloAngularImageBase(Image):
    """Node toolchain + repo clone, shared by every instance of this repo.

    ``dependency()`` returns a *string*, so DockerfileEnhancer.enhance()
    (image.py:265-291) engages: it prepends the BuildKit syntax directive, the
    TARGETARCH/REPO_URL/BASE_COMMIT ARGs, the proxy + cert ENV block and the OCI
    labels, and rewrites the ``RUN git clone ... /home/apollo-angular`` line
    below into the standardized fetch + ``git checkout ${BASE_COMMIT}`` +
    history-hardening block. None of that is written here on purpose.
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

    def dependency(self) -> str | Image:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        # PR-scoped, NOT a bare "base". The base image bakes in a specific
        # BASE_COMMIT (`git checkout ${BASE_COMMIT}` + the scrub's
        # `test "$(git rev-parse HEAD)" = ...` assert), and BASE_COMMIT differs
        # per PR. Images dedupe on image_name():image_tag(), so a shared "base"
        # tag across several PRs of this repo would build only one of them and
        # leave every other PR inheriting the wrong pinned commit.
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

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV CI=true
# Scoped deliberately: this reaches `yarn install` and the ng-packagr build --
# the memory-hungry steps -- but NOT jest. packages/apollo-angular/package.json
# defines `"test": "NODE_OPTIONS=--experimental-modules jest ..."`, and an inline
# assignment REPLACES the inherited NODE_OPTIONS for that process rather than
# appending to it, so the heap flag is dropped there. Raising jest's heap would
# mean patching the repo's own test script, which would break parity with CI
# (`yarn test --ci`); jest completes well inside the default heap (145 cases,
# ~50 s in the reference run), so it is left alone.
ENV NODE_OPTIONS=--max-old-space-size=4096
ENV YARN_CACHE_FOLDER=/root/.cache/yarn

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git python3 make g++ patch \\
    && rm -rf /var/lib/apt/lists/*

RUN node --version && yarn --version

{code}

{self.clear_env}
"""


class ApolloAngularImageDefault(Image):
    """Per-PR image: the patches, the three run scripts, and a warmed install."""

    def __init__(self, pr: PullRequest, config: Config):
        # Defence in depth: the Instance normalizes too, and the transform is
        # idempotent, so this only matters if an ImageDefault is ever built
        # without going through Instance.create().
        self._pr = _normalize_patches(pr)
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ApolloAngularImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            # Already corrected on the record by _normalize_patches(), so the
            # staged files and pr.{fix,test}_patch are the same bytes.
            File(
                ".",
                "fix.patch",
                self.pr.fix_patch,
            ),
            File(
                ".",
                "test.patch",
                self.pr.test_patch,
            ),
            # Integrity guard, COPY'd in reference position (after the patches,
            # before prepare.sh). prepare.sh calls it twice -- once after the
            # reset, once after the checkout -- so a dirty or non-git tree can
            # never be baked into the image.
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail

REPO_DIR=/home/{pr.repo}

cd "$REPO_DIR"

if [ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" != "true" ]; then
    echo "check_git_changes: FAIL - $REPO_DIR is not a git work tree" >&2
    exit 1
fi

# --porcelain ignores .gitignore'd paths, so node_modules/ and
# packages/*/build/ do not trip this once yarn has run.
changes="$(git status --porcelain)"
if [ -n "$changes" ]; then
    echo "check_git_changes: FAIL - working tree is dirty:" >&2
    echo "$changes" >&2
    exit 1
fi

echo "check_git_changes: OK - clean tree at $(git rev-parse HEAD)"
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# yarn classic (lockfile v1, no .yarnrc.yml, no packageManager field) -- the
# copy bundled with the node image. --frozen-lockfile first so a drifted
# lockfile is loud, then a plain install as fallback; the trailing `|| true`
# keeps an optional native postinstall from failing the whole image build.
yarn install --frozen-lockfile || yarn install || true

# Warm the ng-packagr output at base.sha. The run scripts rebuild it after
# patching, but doing it once here makes the incremental rebuild cheap.
yarn workspace apollo-angular build || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdq
git checkout {pr.base.sha}

yarn workspace apollo-angular build

yarn test --ci --verbose
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdq
git checkout {pr.base.sha}

git apply --whitespace=nowarn /home/test.patch

yarn workspace apollo-angular build

yarn test --ci --verbose
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git reset --hard
git clean -fdq
git checkout {pr.base.sha}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

yarn workspace apollo-angular build

yarn test --ci --verbose
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("the-guild-org", "apollo-angular")
class ApolloAngular(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        # Correct the collector's patch split on the record itself, before the
        # harness reads it. Every Instance.create() call site routes through
        # here: build_dataset.py:475, run_evaluation.py:484, report.py:642.
        self._pr = _normalize_patches(pr)
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ApolloAngularImageDefault(self.pr, self._config)

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

    # --- parse_log -------------------------------------------------------
    #
    # `jest --verbose --ci` prints one block per suite:
    #
    #     PASS testing/tests/operation.spec.ts (17.379 s)
    #       TestOperation
    #         ✓ accepts a null body (14 ms)
    #         ○ skipped should run inside Zone
    #
    # Three things make a naive regex wrong here, all observed in real output:
    #
    #  1. The `PASS <file>` line carries a *variable* duration suffix
    #     (`(17.379 s)`) that appears on some stages and not others. It is
    #     stripped, otherwise the same suite would be two different names
    #     across stages and Report.__post_init__ would union them as two
    #     entries (report.py:93-101).
    #  2. ng-packagr, which runs in the same log before jest, prints
    #     `✔ Built apollo-angular/headers` at column 0 using U+2714 HEAVY
    #     CHECK MARK. Jest uses U+2713 CHECK MARK and always indents. Matching
    #     `[✓✔]` would therefore invent 17 phantom passing tests. Only U+2713
    #     and friends are matched, only when indented, and only inside a suite
    #     block.
    #  3. Leaf `it()` text alone is not unique -- http-batch-link.spec.ts
    #     declares `should support headers from context` twice in the same
    #     describe (lines 304 and 332). Ids are therefore
    #     `<file> > <describe...> > <test>`, and a repeat of an identical id
    #     inside one run gets a ` [n]` suffix. The suffix is scoped to that one
    #     id, so adding or removing unrelated tests never renumbers it.
    _ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    _DURATION = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")
    _SUITE_LINE = re.compile(r"^(PASS|FAIL)\s+(\S+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")
    _CASE_LINE = re.compile("^( {2,})([✓✕✗×√○✎])\\s+(.*)$")
    _DESCRIBE_LINE = re.compile(r"^( {2,})(\S.*)$")
    _SKIP_PREFIX = re.compile(r"^(?:skipped|todo)\s+")
    _BLOCK_END = ("Test Suites:", "Tests:", "Snapshots:", "Time:", "Ran all test suites")

    def parse_log(self, test_log: str) -> TestResult:
        clean = self._ANSI.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_suite: str | None = None
        describes: list[tuple[int, str]] = []
        in_suite_block = False
        seen: dict[str, int] = {}

        for raw_line in clean.splitlines():
            line = raw_line.rstrip()

            suite_match = self._SUITE_LINE.match(line)
            if suite_match:
                status, path = suite_match.group(1), suite_match.group(2)
                # Suite-level id: a suite that fails to compile prints no case
                # lines at all, so without this a broken suite would vanish
                # into NONE instead of registering as a failure.
                if status == "PASS":
                    passed_tests.add(path)
                else:
                    failed_tests.add(path)
                current_suite, describes, in_suite_block = path, [], True
                continue

            if not in_suite_block:
                continue

            stripped = line.strip()
            # A blank line, a `●` failure detail header, or the final summary
            # ends the tree. Everything after it is prose, not test names.
            if (
                not stripped
                or stripped.startswith("●")
                or line.startswith(self._BLOCK_END)
            ):
                in_suite_block = False
                continue

            case_match = self._CASE_LINE.match(line)
            if case_match:
                indent = len(case_match.group(1))
                marker = case_match.group(2)
                name = self._DURATION.sub("", case_match.group(3)).strip()

                if marker in "○✎":
                    status = "skip"
                    name = self._SKIP_PREFIX.sub("", name)
                elif marker in "✕✗×":
                    status = "fail"
                else:
                    status = "pass"

                while describes and describes[-1][0] >= indent:
                    describes.pop()

                test_id = " > ".join(
                    [current_suite] + [d[1] for d in describes] + [name]
                )
                seen[test_id] = seen.get(test_id, 0) + 1
                if seen[test_id] > 1:
                    test_id = f"{test_id} [{seen[test_id]}]"

                if status == "pass":
                    passed_tests.add(test_id)
                elif status == "fail":
                    failed_tests.add(test_id)
                else:
                    skipped_tests.add(test_id)
                continue

            describe_match = self._DESCRIBE_LINE.match(line)
            if describe_match:
                indent, name = len(describe_match.group(1)), describe_match.group(2).strip()
                while describes and describes[-1][0] >= indent:
                    describes.pop()
                describes.append((indent, name))

        # TestResult.__post_init__ (test_result.py:82-95) rejects any overlap
        # between the three sets. A suite id can legitimately appear as both
        # (jest retries, or a suite listed twice), so collapse to the
        # strongest verdict: FAIL > SKIP > PASS.
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
