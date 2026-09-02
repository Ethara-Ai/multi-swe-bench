"""graphql-hive/graphql-eslint -- era 1 of 2, PR interval 2782 -> 2735 (node 22 / pnpm 9 / vitest 2.x).

Era boundary. At PR 2735's base (007f3f2d85d2) the root package.json gains
    "packageManager": "pnpm@9.14.2", "engines": {"node": ">=16", "pnpm": ">=9.0.6"}
    "test": "turbo run test"
pnpm-lock.yaml moves from lockfileVersion 5.4 to 9.0, vitest jumps 0.29 -> 2.1
and the suite moves out of the repo root into packages/plugin. Every PR at or
below 1540 still pins lockfileVersion 5.4, carries no packageManager field and
runs `vitest .` from the repo root, so the two groups cannot share a base image:

    graphql_eslint_2782_to_2735.py  this file   node:22, corepack pnpm 9.14.x
    graphql_eslint_1540_to_1346.py  3 PRs       node:18, pnpm 7.33.7

Registration. This era answers to `graphql-hive/graphql_eslint_2782_to_2735`,
which Instance.create() (instance.py:41-49) builds only from a dataset row
carrying number_interval="graphql_eslint_2782_to_2735".

Image layout. ONE base image per repo config -- `base-2782-to-2735`, shared by
both PRs in the range -- and a thin `pr-<N>` on top that only stages the two
patches plus the five scripts and runs prepare.sh. The base owns the clone, the
pin and the history scrub, so the split matches the canonical shape the
Dockerfile QC checklists grade (D11/D13/D14 are base items, P1-P9 assume the PR
layer is a pure COPY + prepare layer) WITHOUT going to one image per PR.

Where the prune lives, and why not in the base. A range-shared tag is built
exactly once, from the FIRST PR in the dataset -- 2735, the older. So the base
does own the ${BASE_COMMIT} pin and the scrub, but its scrub deliberately omits
`git gc --prune=now --aggressive` and `git repack` (see _scrub_without_prune).
Those two steps are the only ones that DELETE objects, and running them at
2735's commit would destroy 2782's base commit; its prepare.sh would then die
on `git checkout` (R10).

Everything that makes history UNREACHABLE stays in the base: origin removed,
every ref deleted, both reflogs expired, alternates removed, `gc.auto 0` set so
no background collection touches them, plus all four integrity asserts. The
other base commit therefore survives as a dangling object that nothing
advertises -- checkout-by-full-SHA still works, which is exactly what
prepare.sh needs and nothing more.

The prune then runs one layer up, at the end of each pr-<N>'s prepare.sh, right
after that PR checks out its own SHA. That is the only place a single
BASE_COMMIT is known, HEAD is that commit and there are no refs, so the
collection removes every other commit in the range. prepare.sh re-asserts all
four invariants afterwards. The graded image -- pr-<N>, not the base -- is
therefore fully pruned, and its runtime tree cannot reach any commit but its
own.

The base keeps a string dependency() so DockerfileEnhancer supplies the syntax
directive, the proxy/CA/ENV wiring, the OCI labels and the CA-cert farm. The
clone below is written already-parameterised as `git clone "${REPO_URL}"`,
which _standardize_repo_fetch's negative lookahead (image.py:379-384) skips, so
the enhancer does NOT rewrite it into a ${BASE_COMMIT} checkout -- the
WORKDIR / reset / checkout / hardening / CMD tail is emitted here instead, once.

Test names. All three stages emit the vitest JSON report between
===VITEST_JSON_BEGIN=== / ===VITEST_JSON_END=== markers and parse_log() reads
that instead of the console reporter. This repo's RuleTester cases are named
after multi-line GraphQL documents, so every console reporter -- verbose, tap,
junit -- spreads one test name over a dozen lines and a line-by-line parser
recovers nothing (many titles start with a newline, so the reporter's first line
ends at "> " with no name on it at all). The JSON report escapes them, and
parse_log() collapses the whitespace to one line, identically in every stage.
"""

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The two object-destroying steps of Image._HARDENING_BLOCK, exactly as they
# appear in it (top-level gc, top-level repack, submodule gc).
_PRUNE_STEPS = (
    "    git gc --prune=now --aggressive; \\\n",
    "    git repack -a -d -l --quiet; \\\n",
    "            git gc --prune=now --aggressive; \\\n",
)


def _scrub_without_prune() -> str:
    """Image._HARDENING_BLOCK with the gc/repack steps removed.

    Everything that makes history *unreachable* stays -- origin removal, ref
    deletion, reflog expiry, the alternates removal, the three local configs and
    all four integrity asserts. Only the steps that actually DELETE objects come
    out, because a base shared by a whole PR range cannot prune: it is built
    once, from the first PR in the range, and pruning to that commit would
    destroy the later PRs' base commits. The prune runs one layer up instead, at
    the end of prepare.sh, where a single BASE_COMMIT is known.

    Raises if the harness block ever stops containing these exact lines, so this
    fails loudly at generation time rather than silently shipping a base that
    still prunes.
    """
    block = Image._HARDENING_BLOCK
    for step in _PRUNE_STEPS:
        if step not in block:
            raise ValueError(
                f"Image._HARDENING_BLOCK no longer contains {step!r}; "
                "_scrub_without_prune() needs updating"
            )
        block = block.replace(step, "", 1)
    return block.rstrip("\n")


class GraphqlEslintImageBase(Image):
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
        # .github/workflows/test.yml pins nodeVersion 22 for this era; the pnpm
        # version comes from the packageManager field (9.14.2 at PR 2735,
        # 9.14.3 at PR 2782) through the corepack shim, which prepare.sh
        # materialises at build time.
        return "node:22"

    def image_tag(self) -> str:
        return "base-2782-to-2735"

    def workdir(self) -> str:
        return "base-2782-to-2735"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # This shared base owns the clone, the ${BASE_COMMIT} pin and the scrub,
        # so there is exactly one commit ARG in the rendered file -- the one the
        # enhancer injects. build_dataset.py:626-630 supplies it because
        # dependency() returns a string.
        #
        # The clone is written already-parameterised, so
        # DockerfileEnhancer._standardize_repo_fetch skips it -- its Pattern 2
        # carries a `(?!"\\$\\{REPO_URL\\}")` lookahead (image.py:379-384). The
        # tail below is therefore emitted exactly once, by this method.
        #
        # The scrub here is Image._HARDENING_BLOCK MINUS its gc/repack steps
        # (see _scrub_without_prune). This tag is shared by the whole range and
        # is built once, from the first PR in the dataset -- 2735 -- so a prune
        # anchored at ${BASE_COMMIT} would delete 2782's base commit and its
        # prepare.sh would die on `git checkout` (R10). Dropping only the prune
        # leaves that commit present but UNREACHABLE: origin is gone, every ref
        # is deleted and both reflogs are expired, so nothing advertises it, and
        # `git config --local gc.auto 0` stops a background gc from collecting
        # it behind our back. Each pr-<N> then checks out its own SHA and runs
        # the prune itself, at the end of prepare.sh.
        #
        # apt and `corepack enable` precede the clone so the toolchain layer
        # stays cacheable and the network boundary is correct (D17): the proxy
        # ENV and the CA-cert farm the enhancer injects after FROM are already
        # in place before the first network RUN. corepack only installs the
        # shim; the pinned pnpm (9.14.2 at 2735, 9.14.3 at 2782) is downloaded
        # from the packageManager field by prepare.sh in the PR layer, which is
        # the layer that knows the commit.
        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        hardening = _scrub_without_prune()

        # Sections are joined rather than interpolated so an empty global_env /
        # clear_env leaves no blank-line run in the rendered file.
        sections = [
            f"FROM {image_name}",
            self.global_env,
            "ENV CI=true\nENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0",
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            "RUN corepack enable",
            fetch,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            hardening,
            self.clear_env,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


class GraphqlEslintImageDefault(Image):
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
        return GraphqlEslintImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # `pnpm test` is `turbo run test`, and packages/plugin is the only
        # workspace with a test script. vitest runs from that package because
        # __tests__/examples.spec.ts spawns `eslint` off PATH: from
        # packages/plugin the shim resolves that package's own eslint, whose
        # ESLintRCWarning text is the one the spec's stderr scrubber knows
        # about. --no-file-parallelism is vitest 2's single-worker switch (R14).
        # The exit code is carried to the end of the script rather than
        # swallowed, so a runner that fails to start still fails the stage.
        build_command = "pnpm turbo run build --filter='!website' || true"
        test_command = """cd /home/{repo}/packages/plugin
rm -f /home/vitest-results.json
rc=0
pnpm exec vitest run --no-file-parallelism --reporter=json \\
    --outputFile=/home/vitest-results.json || rc=$?""".format(repo=self.pr.repo)

        # Install and build run in all three run scripts, not only in
        # prepare.sh: examples.spec.ts lints the examples/ workspaces through
        # the plugin's built dist, so a fix patch that touches
        # packages/plugin/src is invisible until the plugin is rebuilt, and PR
        # 2735's fix patch adds three devDependencies to examples/vue-code-file
        # that its own gold test needs. Both commands are identical in run.sh,
        # test-run.sh and fix-run.sh, so neither can skew the comparison.
        run_script = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_NO_WARNINGS=1
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{repo}
{{apply}}
pnpm install --no-frozen-lockfile || true
{build}
{test}
echo "===VITEST_JSON_BEGIN==="
cat /home/vitest-results.json 2>/dev/null || true
echo
echo "===VITEST_JSON_END==="
exit $rc

""".format(repo=self.pr.repo, build=build_command, test=test_command)

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
export CI=true
export NODE_NO_WARNINGS=1
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The shared base could not prune -- it is built once for the whole PR range, so
# collecting there would have deleted the other PR's base commit. It left it
# present but unreachable instead (no origin, no refs, no reflog, gc.auto 0).
# HEAD is now THIS PR's base commit and there are still no refs, so this is the
# layer that can finally collect: everything outside this commit's history goes.
#
# Do NOT drop these two lines. They are the only prune in the whole pipeline,
# and the base's four integrity asserts cannot stand in for them: measured on
# this repo, a base-scrubbed tree with no gc passes all four asserts while still
# holding 2605 commit objects against 1088 in HEAD's history -- 1517 extra
# commits, including the fix commits for these PRs. `rev-list --all` walks refs
# and HEAD only, so it never sees dangling objects.
git gc --prune=now --aggressive
git repack -a -d -l --quiet

# --frozen-lockfile first so the pinned eslint reaches the examples/ workspaces;
# examples.spec.ts compares eslint's stderr against a hardcoded warning string
# and a floating resolution changes that text.
pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile || true
{build}

""".format(pr=self.pr, build=build_command),
            ),
            File(
                ".",
                "run.sh",
                run_script.format(apply=""),
            ),
            File(
                ".",
                "test-run.sh",
                run_script.format(
                    apply="""if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi""".format(repo=self.pr.repo)
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                run_script.format(
                    apply="""if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi""".format(repo=self.pr.repo)
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # A thin PR layer, per the canonical split: the clone, the pin and the
        # history scrub all belong to the shared base-2782-to-2735, so this file
        # only stages the two patches plus the five scripts and runs prepare.sh
        # once. It must not re-clone, re-checkout, re-apt or re-scrub, and it
        # must not touch the proxy/CA env it inherits (P9).
        #
        # prepare.sh below carries the one job the shared base could not do:
        # after checking out THIS PR's base commit it runs the gc/repack the
        # base omitted, so the graded image ends up pruned to a single commit.
        # That is still a checkout inside an existing clone -- not a re-fetch,
        # not a re-scrub of anything the base already did.
        #
        # This image chains to an Image object, so DockerfileEnhancer.enhance()
        # returns the text verbatim (image.py:315-316) -- nothing is injected
        # and nothing here would be rewritten.
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        # prepare.sh re-asserts the pinned tree at PR-build time (reset ->
        # clean-check -> checkout <base sha> -> clean-check) and then warms the
        # pnpm store and the build output. Run exactly once (P4).
        prepare_commands = "RUN bash /home/prepare.sh"

        # Sections are joined rather than interpolated so an empty global_env /
        # clear_env leaves no blank-line run in the rendered file.
        sections = [
            f"FROM {name}:{tag}",
            self.global_env,
            copy_commands,
            prepare_commands,
            self.clear_env,
        ]
        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("graphql-hive", "graphql_eslint_2782_to_2735")
class GRAPHQL_ESLINT_2782_TO_2735(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GraphqlEslintImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        begin = clean_log.rfind("===VITEST_JSON_BEGIN===")
        end = clean_log.rfind("===VITEST_JSON_END===")
        if begin == -1 or end <= begin:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        payload = clean_log[begin + len("===VITEST_JSON_BEGIN===") : end].strip()
        try:
            report = json.loads(payload)
        except ValueError:
            report = {}

        for file_report in report.get("testResults") or []:
            # vitest reports absolute paths, and this era runs the suite from
            # packages/plugin, so its own paths are package-relative. report.py
            # matches a test name against the patch's file list with
            # `name.startswith(path + " > ")` (report.py:385-395), so the path
            # has to be repo-relative (R20).
            path = str(file_report.get("name") or "").replace("\\", "/")
            marker = f"/home/{self.pr.repo}/"
            index = path.find(marker)
            if index != -1:
                path = path[index + len(marker) :]

            # RuleTester names a case after the document it lints, so one
            # document tested twice under different rule options produces two
            # tests with byte-identical names. Without a suffix they collapse
            # into one entry and a failure in either hides the other. The
            # counter is per file and vitest reports assertions in declaration
            # order under a single worker, so the suffix is the same in all
            # three stages (R3).
            # A file that throws while LOADING never registers a single it(),
            # so vitest reports it with zero assertionResults and its tests
            # simply vanish from that stage -- PR 1540 collects 597 tests at the
            # run stage and 550 at the test stage, because the gold
            # naming-convention spec dies at import: ESLint's RuleTester
            # evaluates its invalid cases at module scope, and without the fix
            # the new options produce no errors, so it throws "Invalid case
            # should have at least one error." before any test exists. Those
            # tests have no names, so they cannot be reported one by one.
            #
            # What CAN be reported is the load itself, which is a real property
            # that broke and got fixed. Emit one sentinel per file in EVERY
            # stage, so it is present in all three and the comparison is a
            # genuine FAIL -> PASS instead of an invisible NONE -> PASS. The
            # discriminator is `message`: non-empty only on a file that failed
            # to collect (0 of 50 files at the run and fix stages, exactly 1 at
            # the test stage). `status` is NOT usable -- vitest reports the
            # broken file as "passed". The sentinel keeps the "<path> > ..."
            # shape so report.py can still resolve it to its own file (R20).
            message = str(file_report.get("message") or "").strip()
            assertions = file_report.get("assertionResults") or []
            sentinel = f"{path} > [suite loads]"
            if message:
                failed_tests.add(sentinel)
            elif assertions:
                passed_tests.add(sentinel)
            else:
                # Collected cleanly but holds no test -- an empty or fully
                # skipped file. Not a failure, and not a pass either.
                skipped_tests.add(sentinel)

            occurrences: dict[str, int] = {}

            for assertion in assertions:
                titles = [
                    title
                    for title in [
                        *(assertion.get("ancestorTitles") or []),
                        assertion.get("title") or "",
                    ]
                    if title and title.strip()
                ]
                if not titles:
                    continue

                # A title is routinely a multi-line template literal.
                # Collapsing the whitespace is what keeps one test on one line
                # and byte-identical across the three stages (R3).
                name = re.sub(r"\s+", " ", f"{path} > " + " > ".join(titles)).strip()
                occurrences[name] = occurrences.get(name, 0) + 1
                if occurrences[name] > 1:
                    name = f"{name} [dup#{occurrences[name]}]"

                status = assertion.get("status") or ""
                if status == "failed":
                    failed_tests.add(name)
                elif status == "passed":
                    passed_tests.add(name)
                else:
                    # "pending", "skipped" and "todo".
                    skipped_tests.add(name)

        # The sets must be disjoint or TestResult raises (R2). Failure wins.
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
