"""HospitalRun/hospitalrun-frontend harness config.

React app on Create React App (react-scripts), Jest tests, npm.
Node 16 is required: cheerio pulls parse5-parser-stream, which uses
`node:`-prefixed imports.

The repo ships NO lockfile — package-lock.json is gitignored — so every install
resolves current versions of a 2020 dependency tree and `npm ci` is unavailable.
Two dependencies must therefore be pinned explicitly in prepare.sh; see the
comments there.

Two harness behaviours shape this file. Neither may be worked around by editing
the harness, so the config accommodates them:

1. `DockerfileEnhancer._inject_final_sanitize()` appends the hardening block to
   any Dockerfile containing the substring `git clone`. Writing the base clone as
   `git -C /home clone ...` is the same operation to git and does not match that
   substring.

2. `build_dataset.py` passes REPO_URL / BASE_COMMIT as build args ONLY when
   `dependency()` returns a str — i.e. only to the base. A PR layer referencing
   `${BASE_COMMIT}` would expand it to "" and check out the default branch, so
   the PR layer writes its sha literally.

`git clean` must not carry `-x`: `npm install` writes node_modules/, which
.gitignore covers, and `-x` would delete it between the acts — every stage would
then report 0/0/0 with no error. `-e node_modules` guards against a future commit
that stops ignoring it.
"""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_JEST_REPORT_JS = """\
// Emits one line per test in the harness's canonical form:
//
//     jest:<file> > <describe path> > <test name> PASSED|FAILED|SKIPPED
//
// The alternative is parsing jest's human output: ANSI-coloured marker glyphs,
// with the describe path reconstructed from indentation. That path has to be
// exact -- a test name repeated across sibling describes collapses into one id
// otherwise, and a merged id whose instances disagree is recorded as FAILED,
// a wrong verdict for the instance that passed. Measured on this repo: 490
// results collapsed to 464 distinct ids that way.
//
// The reporter already holds the full path in `ancestorTitles`, so emitting it
// directly removes that class of error rather than compensating for it.
class HarnessReporter {
  onTestResult(_contexts, result) {
    const file = result.testFilePath.replace(/^.*?\\/home\\/[^/]+\\//, '');

    // A suite that could not be imported produces NO per-test results. Emit one
    // line for the file so the act still carries that signal instead of going
    // silently empty.
    if (result.testResults.length === 0) {
      if (result.testExecError || result.failureMessage) {
        process.stdout.write(`jest:${file} FAILED\\n`);
      }
      return;
    }

    for (const t of result.testResults) {
      const path = [...t.ancestorTitles, t.title].join(' > ');
      const status = t.status === 'passed'  ? 'PASSED'
                   : t.status === 'failed'  ? 'FAILED'
                   :                          'SKIPPED';
      process.stdout.write(`jest:${file} > ${path} ${status}\\n`);
    }
  }
}
module.exports = HarnessReporter;
"""


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail
cd /home/{repo}
# node_modules is gitignored, so it must not count as a change. Anything else
# dirty means an act mutated the tree and the next act would not start clean.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "WORK TREE DIRTY:"
    git status --porcelain --untracked-files=no
    exit 1
fi
"""

# The real dependency install. Deliberately NOT suffixed with `|| true`: under
# prepare.sh performs the REAL install, so a failure must
# fail the build rather than yield an image where every act scores 0/0/0.
# (A swallowed install failure yields an image where every act scores 0/0/0
# with no error, which is far worse than a loud build failure.)

# One shared test invocation, so the three acts differ ONLY by which patches are
# applied. CI=true stops react-scripts watching; --watchAll=false is belt and
# braces. `|| true` here is correct and load-bearing: a failing test suite is the
# SIGNAL in the test act, not an error.
_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

npx react-scripts test --watchAll=false --maxWorkers=2 \\
    --reporters=default --reporters=/home/jest_report.js 2>&1 || true
"""

_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail
cd /home/{repo}
for p in "$@"; do
    git apply --whitespace=nowarn "$p"
done
"""


class HospitalRunFrontendImageBase(Image):
    """Clone-only base, shared by every PR in the dataset.

    Its content is commit-independent, so one `:base` tag for all five PRs is
    correct here: the content is identical for every PR.
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
        return "node:16-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        # Every COPY belongs to the PR layer under this layout.
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{self.pr.repo}; \\
        if git -C /home clone "${{REPO_URL}}" {self.pr.repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{self.pr.repo}/.git

{self.clear_env}

CMD ["/bin/bash"]
"""


class HospitalRunFrontendImageDefault(Image):
    """Per-PR layer: context files, the checkout, the scrub, then prepare.sh."""

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
        return HospitalRunFrontendImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "jest_report.js", _JEST_REPORT_JS),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo)),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdq -e node_modules
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

# cheerio >= 1.1.0 pulls parse5-parser-stream, which uses `node:`-prefixed
# imports that jest's resolver in this CRA version cannot handle. Pinning it is
# what keeps the suite collectable at these commits.
npm pkg set overrides.cheerio=1.0.0-rc.12 2>/dev/null || \\
  node -e "var p=require('./package.json');p.overrides=p.overrides||{{}};p.overrides.cheerio='1.0.0-rc.12';require('fs').writeFileSync('./package.json',JSON.stringify(p,null,2)+'\\n')"

# shortid: ERA PIN, and the single most load-bearing line in this file.
#
# The repo ships NO lockfile (package-lock.json is gitignored), so every install
# resolves TODAY's versions of a 2020 dependency tree. package.json declares
# `shortid: ^2.2.15`, which in 2020 resolved to 2.2.15 -> nanoid ^2.1.0 (pure
# CommonJS). Today the same range resolves to 2.2.17 -> nanoid ^3.3.8, a dual
# ESM/CJS package whose `browser` exports map sends CRA's jsdom-based jest to
# index.browser.cjs, where the CJS destructure of `customRandom` yields
# undefined. Every clients/db/*Repository test then dies with
#     TypeError: customRandom is not a function
# in ALL THREE acts -- an environment failure wearing the costume of a code one.
#
# MEASURED on pr-2087: PatientRepository.test.ts 11 failed -> 17 passed / 0 failed.
#
# It has to be `npm pkg set dependencies.shortid`, not an override: shortid is a
# DIRECT dependency, and npm rejects an override that narrows one --
#     npm ERR! Override for shortid@^2.2.15 conflicts with direct dependency
npm pkg set dependencies.shortid=2.2.16

# @hospitalrun/components: ERA PIN #2, derived per-PR from the commit's OWN
# package.json rather than a hardcoded table.
#
# Each commit declares a different floor -- ^0.34.0 (#1863,#1882), ^1.2.0 (#1956),
# ^1.4.0 (#2040), ^1.5.0 (#2087) -- but with no lockfile every one of them
# installs 1.16.1 (Aug 2020), whose Navbar renders an extra child. The suite then
# fails with
#     Expected: "actions.list"   Received: [undefined, "actions.list"]
# in ALL THREE acts.
#
# The correlation is exact and is what identifies the cause: the two PRs that do
# NOT fail are precisely the two declaring ^0.34.0, where npm's caret cannot
# cross to 1.x. The three declaring ^1.x drift the whole 1.x line and all fail.
#
# Stripping the caret pins each PR to the version its own package.json asks for,
# so one recipe serves all five without an era split.
#
# MEASURED on pr-2087: Navbar.test.tsx 4 failed -> 14 passed / 0 failed.
COMP_RANGE=$(npm pkg get 'dependencies.@hospitalrun/components' | tr -d '"' | tr -d ' ')
COMP_PIN=$(printf '%s' "$COMP_RANGE" | sed 's/^[\^~]//')
if [ -n "$COMP_PIN" ] && [ "$COMP_PIN" != "undefined" ]; then
    npm pkg set "dependencies.@hospitalrun/components=$COMP_PIN"
    echo "pinned @hospitalrun/components $COMP_RANGE -> $COMP_PIN"
fi

npm install --legacy-peer-deps --ignore-scripts

node --version
npm --version
test -d node_modules

# package.json was edited by the two `npm pkg set` calls above; restore it so the
# tree is clean for the acts, while KEEPING node_modules -- which is the whole
# point of resolving the tree at image-build time.
git checkout -- package.json 2>/dev/null || true
git reset --hard
git clean -fdq -e node_modules
bash /home/check_git_changes.sh
""".format(repo=repo, sha=sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # Literal sha, not ${BASE_COMMIT} — see note 2 at the top of this file.
        sha = self.pr.base.sha
        # The hardening block is DERIVED from the harness's own definition rather
        # than retyped. Copying it by hand is how the submodule scrub went missing
        # once: the superproject got scrubbed while every submodule kept its full
        # history and its own `origin`. Deriving makes that omission impossible --
        # if the harness gains a step, this layer gains it too.
        #
        # `_HARDENING_BLOCK` is READ from harness.image; nothing in core is
        # modified. It is parameterised on ${BASE_COMMIT}, which the harness only
        # supplies to a base image, so the PR layer substitutes the literal sha.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()
        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("HospitalRun", "hospitalrun-frontend")
class HospitalRunFrontend(Instance):
    """Resolves the dataset's own org/repo key.

    Other configs for this repo register under synthetic range keys, which a
    dataset carrying repo="hospitalrun-frontend" never matches. This class
    registers the key the dataset actually uses.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return HospitalRunFrontendImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        """Read the canonical result lines emitted by jest_report.js.

            jest:<file> > <describe path> > <test name> PASSED|FAILED|SKIPPED

        The reporter supplies the fully-qualified name, so this parser does not
        reconstruct anything: no ANSI stripping, no marker glyphs, no describe
        stack inferred from indentation. Everything that made the previous
        approach fragile lived in that reconstruction.

        A suite that cannot be imported yields no per-test lines; the reporter
        emits `jest:<file> FAILED` for it so the act still carries the signal.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # A result line is a NAME followed by the status keyword. The name must
        # look like a test id -- either `<prefix>:<...>` (what jest_report.js
        # emits) or something containing ` > ` (a suite-qualified name). Without
        # that requirement any log line ending in the word PASSED would be
        # counted as a test, which is how a parser invents results.
        line_re = re.compile(
            r"^(?P<name>\S+(?::|(?=.* > )).*?) (?P<status>PASSED|FAILED|SKIPPED)$")
        # jest colourises its own output, and the two streams share stdout.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        for line in test_log.splitlines():
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name = m.group("name").strip()
            status = m.group("status")
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # TestResult.__post_init__ enforces disjoint sets: a retry that later
        # passed must not outrank the failure, and a skip must not outrank one.
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
