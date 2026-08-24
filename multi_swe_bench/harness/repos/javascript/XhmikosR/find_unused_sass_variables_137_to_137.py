"""XhmikosR/find-unused-sass-variables -- era 1 of 1, PR interval 137 -> 137 (Node 16 / xo + ad-hoc node script).

Era boundary. The dataset holds a single PR, #137, whose base (ade55b164a04)
declares

    "engines": { "node": ">=10" }
    "scripts": { "test": "npm run xo && npm run test:integration",
                 "test:integration": "node tests/integration.js" }

and whose CI matrix (.github/workflows/test.yml) is node [10, 12, 14, 16]. The
fix patch moves that floor to ">=12.19" because it introduces
Promise.allSettled, so node:16 is the one image that satisfies both the base
commit and the patched tree. There is no second era yet; the file is still
named for its PR range (R24) so that adding one later touches nothing that
already exists.

    find_unused_sass_variables_137_to_137.py   this file   1 PR   node:16

Test command. `npm test` is `xo && node tests/integration.js`. Only the second
half is a test -- xo is a linter, emits no test names, and would abort the stage
under `set -e` before any test ran -- so all three stages execute
`node tests/integration.js` through one shared /home/run-tests.sh (R3 by
construction). tests/integration.js is not a framework: it prints
`Running <type> integration tests...` then `Tests passed!`, and signals failure
with a non-zero exit. run-tests.sh converts that exit code into a
`fusv-suite-result:` marker line so the suite itself is a parseable test.

Observed stage output (node:16, verified in a container before this file was
written):

    run    Running integration tests... / Tests passed!            exit 0
    test   Running Sync integration tests... / Tests passed!
           TypeError: fusv.findAsync is not a function             exit 1
    fix    Running Sync integration tests... / Tests passed!
           Running async integration tests... / Tests passed!      exit 0

which parses to `tests/integration.js > integration suite` PASS->FAIL->PASS
(f2p) and `tests/integration.js > async` NONE->NONE->PASS (n2p). Names are
prefixed with the repo-relative test file so report.py's
_test_name_matches_files resolves them (R20).

No patch sanitiser (R19) is needed: both patches were confirmed to apply with
`git apply` at ade55b164a04, and neither contains a binary hunk nor a
generated-output path. The fix patch touches README.md, cli.js, index.js and
package.json; the test patch touches .github/workflows/test.yml and
tests/integration.js -- a disjoint set, so report.py's step-5 tamper guard
cannot trip.

Image split (revised 2026-08-24 after Dockerfile QC). The base was originally a bare
node:16 environment tagged `base-node16`, shared by every PR on this toolchain, with
the clone, the BASE_COMMIT pin and the history scrub pushed down into the per-PR
image (the old R10 rationale). QC against DOCKERFILE_QC_PROMPT.md failed that split:
the D-series requires the BASE file to own D11 (clone), D12/D13 (pin) and D14 (scrub
with its four integrity asserts), and the base owned none of them. The guarantees did
hold at the pair level -- the asserts ran and passed in the PR layer -- but the
artifact tagged as "the base image" contained no repository at all, and the tag did
not carry the PR number.

So the responsibilities are now where the standard puts them: the base clones, pins
and scrubs, and is tagged `base-pr-<N>`; the PR layer only stages patches and
run-scripts and runs prepare.sh. The cost is the shared base -- one base image per PR
instead of one per era. That is a real cost for a multi-PR repo and a nil cost here,
where the era holds exactly one PR.

Registration. This era answers to `XhmikosR/find_unused_sass_variables_137_to_137`,
which Instance.create() (instance.py:41-49) builds only from a dataset row
carrying number_interval="find_unused_sass_variables_137_to_137". The shipped
raw dataset carries no number_interval, so the plain key
`XhmikosR/find-unused-sass-variables` is aliased onto the same class at the
bottom of this file (R26 / §17.4 option 2) -- correct here because one era
serves every row.
"""

import json as _fusv_json
import re
from typing import Optional, Union

from multi_swe_bench.harness.dataset import Dataset as _FusvDataset
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class FindUnusedSassVariablesImageBase(Image):
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
        return "node:16"

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

        # No apt block: node:16 already ships git 2.20.1 and ca-certificates, and
        # nothing in this repo's dependency tree compiles native code (chalk,
        # commander, glob, escape-string-regexp, postcss, postcss-scss, xo are all
        # pure JS), so `npm ci` succeeds on the stock image. node:16 is
        # bullseye-based, so R11's archive.debian.org rewrite would only matter if
        # apt were used -- and skipping apt also keeps the base off Debian 11's
        # end-of-LTS repo cliff.
        #
        # The clone lives HERE, not in the per-PR image. dependency() returns a
        # string, so DockerfileEnhancer processes this file: _standardize_repo_fetch
        # matches the hardcoded-URL clone below and rewrites it into the canonical
        # block -- `clone "${REPO_URL}"` + WORKDIR + `git reset --hard` +
        # `git checkout ${BASE_COMMIT}` + the hardening block with its four
        # integrity asserts + `CMD ["/bin/bash"]`. That is the whole point: the
        # generator owns the pin-and-scrub contract instead of it being hand-written
        # one layer down.
        #
        # Consequence, accepted deliberately: a base that checks out a specific
        # commit can no longer be shared across PRs, so image_tag() is now
        # `base-pr-<N>` and the R10 shared-era-base design is retired. BASE_COMMIT
        # reaches this build because build_dataset passes REPO_URL/BASE_COMMIT as
        # build args whenever dependency() is a string.
        #
        # No refs/pull fallback is needed here (contrast R12 in prepare.sh):
        # ade55b164a04 is an ancestor of the default branch, so a plain clone
        # reaches it and the enhancer's bare `git checkout ${BASE_COMMIT}` resolves.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{self.clear_env}

RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}
"""


class FindUnusedSassVariablesImageDefault(Image):
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
        return FindUnusedSassVariablesImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# R12: a base commit that lives only on refs/pull/* is not in a plain clone.
# Fetch it on demand, then delete the temp refs so the hardening block's
# `rev-list --all == rev-list HEAD` assertion still holds.
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

# Build time is where the network lives (R16). package-lock.json is committed,
# so `npm ci` is the reproducible path; `npm install` is only a fallback for a
# base commit whose lockfile is out of sync with package.json.
npm ci || npm install || true

# Cache-warming run: also proves at build time that the suite executes.
node tests/integration.js || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{pr.repo}

# tests/integration.js is a plain node script, not a test framework: it prints
# `Running <type> integration tests...` / `Tests passed!` and reports failure
# only through its exit status. Turn that status into a marker line so the
# suite as a whole is visible to parse_log, then exit 0 -- the pass/fail signal
# is the marker, not this script's exit code, and the three callers run under
# `set -e`.
rc=0
node tests/integration.js || rc=$?

if [ "$rc" -eq 0 ]; then
    echo "fusv-suite-result: PASS"
else
    echo "fusv-suite-result: FAIL"
fi

exit 0

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
bash /home/run-tests.sh

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

        prepare_commands = "RUN bash /home/prepare.sh"

        # This layer stays thin, and that is the contract: the repo is already
        # cloned, pinned to BASE_COMMIT and history-scrubbed by the base image, so
        # there is no clone, no checkout and no hardening block here. Re-doing any
        # of it would duplicate a guarantee the base already earned.
        #
        # What remains is exactly the staging job: drop the two patches and the
        # run-scripts into /home/, then run prepare.sh once to re-assert the
        # pristine baseline (reset -> clean assert -> checkout -> clean assert) and
        # warm the npm cache at build time.
        #
        # Dropped (2026-08-24): `git config --global --add safe.directory
        # /home/<repo>`, which this layer carried from the pre-restructure version.
        # It was inert twice over here -- the container runs as root against a
        # root-owned tree, so git's dubious-ownership guard never fires, and node:16
        # ships git 2.20.1, which predates that guard (added in 2.35.2) and does not
        # implement it at all. Restore it if this era's base is ever bumped to a
        # node image carrying git >= 2.35 AND the harness starts running containers
        # under a non-root uid or bind-mounting the tree; until both are true it is
        # a layer that buys nothing.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home

{self.clear_env}

"""


@Instance.register("XhmikosR", "find_unused_sass_variables_137_to_137")
class FIND_UNUSED_SASS_VARIABLES_137_TO_137(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FindUnusedSassVariablesImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # `Running integration tests...` (the unpatched form) deliberately does
        # not match: it carries no type word, so the baseline stage contributes
        # only the suite marker and never a name the patched stages cannot also
        # emit.
        re_running = re.compile(r"^Running (\S+) integration tests\.\.\.$")
        re_passed = re.compile(r"^Tests passed!$")
        re_suite = re.compile(r"^fusv-suite-result: (PASS|FAIL)$")

        # runTests() prints its banner and only then its verdict, so the two
        # lines have to be paired. The pending name is bounded to one: a second
        # banner, or end of log, resolves the previous one as failed -- which is
        # exactly what happens when runTests() throws after announcing itself.
        pending = None

        for line in clean_log.splitlines():
            line = line.strip()

            running_match = re_running.match(line)
            if running_match:
                if pending is not None:
                    failed_tests.add(pending)
                pending = f"tests/integration.js > {running_match.group(1)}"
                continue

            if pending is not None and re_passed.match(line):
                passed_tests.add(pending)
                pending = None
                continue

            suite_match = re_suite.match(line)
            if suite_match:
                suite_name = "tests/integration.js > integration suite"
                if suite_match.group(1) == "PASS":
                    passed_tests.add(suite_name)
                else:
                    failed_tests.add(suite_name)

        if pending is not None:
            failed_tests.add(pending)

        # R2 -- the sets must be disjoint or TestResult raises. Failure wins.
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


# R26 / §17.4 option 2: the shipped raw dataset carries no `number_interval`,
# so Instance.create() computes the plain `XhmikosR/find-unused-sass-variables`
# key. Alias it onto the single era, which is correct here because that era's
# toolchain fits every PR in the dataset.
Instance.register("XhmikosR", "find-unused-sass-variables")(
    FIND_UNUSED_SASS_VARIABLES_137_TO_137
)


# ---------------------------------------------------------------------------
# Dataset field completion -- `lang` and `instance_id`.
#
# validate_dataset rejects every dataset this pipeline generates with two errors:
#
#     field 'instance_id' is empty or missing
#     lang is empty or missing
#
# Neither field is something a repo config can normally reach. gen_report builds
# each row with Dataset.build(pr, report), which copies `lang` straight off the
# raw PullRequest -- null in this repo's raw file -- and never writes
# `instance_id` at all, because PullRequest has no such field for the raw value
# (`XhmikosR__find-unused-sass-variables-137`, which IS present in the raw JSON)
# to land in. The row is then serialised with Dataset.json(), which emits
# declared dataclass fields only.
#
# Fixing that in dataset.py/pull_request.py is out of bounds: those files are
# shared by every registered instance. So the completion is done here instead,
# using the same shim idiom repo configs in this tree already apply to
# Instance.create and PullRequest.from_json (see e.g.
# repos/typescript/tailwindlabs/tailwindcss.py:762-775) -- capture __func__,
# delegate, guard against double-patching.
#
# Scope is the point: rows for any other org/repo are returned untouched by the
# first branch, so this cannot alter another instance's output. `lang` is a
# declared field and is simply set. `instance_id` is not, so it is injected at
# serialisation time by wrapping this row's own json() -- an instance attribute,
# so no other Dataset object is affected. Reading such a row back is safe:
# Dataset.from_json goes through schema().loads(), which drops unknown keys (the
# same reason the raw file's 30-odd extra GitHub keys load cleanly today).
#
# Remove this block if dataset.py ever populates both fields itself.
# ---------------------------------------------------------------------------
if not getattr(_FusvDataset, "_fusv_dataset_fields_shim", False):
    _fusv_orig_build = _FusvDataset.build.__func__

    def _fusv_build(cls, pr, report):
        data = _fusv_orig_build(cls, pr, report)

        if (
            getattr(pr, "org", "") != "XhmikosR"
            or getattr(pr, "repo", "") != "find-unused-sass-variables"
        ):
            return data

        if not getattr(data, "lang", ""):
            data.lang = "javascript"

        # Prefer the id the raw dataset already carries; fall back to the same
        # `<org>__<repo>-<number>` shape validate_dataset --enrich constructs.
        instance_id = (
            getattr(pr, "instance_id", "") or f"{pr.org}__{pr.repo}-{pr.number}"
        )

        _fusv_row_json = data.json

        def _fusv_json_with_instance_id(*args, **kwargs):
            payload = _fusv_json.loads(_fusv_row_json(*args, **kwargs))
            payload["instance_id"] = instance_id
            return _fusv_json.dumps(payload, ensure_ascii=False)

        data.json = _fusv_json_with_instance_id
        return data

    _FusvDataset.build = classmethod(_fusv_build)
    _FusvDataset._fusv_dataset_fields_shim = True
