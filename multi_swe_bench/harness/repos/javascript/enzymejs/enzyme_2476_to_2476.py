"""enzymejs/enzyme -- era 1 of 1, PR interval 2476 -> 2476 (node 14 / npm 6 / lerna 2 / mocha 3).

Era boundary. The dataset holds one PR, #2476, whose base commit 2dbc81623ef4
(2020-12-09) pins the toolchain this file targets: the root package.json declares
`lerna@^2.11.0` and `mocha@^3.5.3`, the workspace is bootstrapped with
`lerna bootstrap` rather than npm workspaces, and `test:only` is
`mocha --recursive packages/enzyme-test-suite/build`. There is no second era to
separate it from; the range shape is used anyway (R24) so that a PR on a later
toolchain becomes a new file rather than a rename of this one:

    enzyme_2476_to_2476.py    this file    node:14    1 PR

Registration. The dataset row carries no `number_interval` -- PullRequest.from_json
on the raw row yields "" -- so Instance.create() (instance.py:41-49) computes the
PLAIN key "enzymejs/enzyme". Per R26/§17.4 option 2, that plain key is registered
as an alias onto this class at the bottom of the file, alongside the range key
"enzymejs/enzyme_2476_to_2476". The alias is correct here because this era serves
every row in the dataset: there is no older toolchain a plain-key row could belong
to, so nothing can be mis-routed onto the wrong base image.

Two levels. `<org>_m_<repo>:base-pr-2476` is the heavy environment: node:14 plus the
clone, the `${BASE_COMMIT}` checkout and the git-history scrub -- all three injected by
DockerfileEnhancer._standardize_repo_fetch from the single hard-coded clone line in
ImageBase.dockerfile(). `<org>_m_<repo>:pr-2476` is the thin layer on top: it COPYs the
two patches and the five scripts and runs prepare.sh once, nothing else.

That split is the shape the tree's own deliverables use (compare the servicecomb base in
output4, which clones and scrubs in `base-pr-827` and keeps `pr-827` to five lines) and
the shape DOCKERFILE_QC_PROMPT.md audits -- D11/D12/D13/D14 expect the clone, the pin and
the scrub in the BASE file, and P9 fails a PR layer that re-implements them. It is the
opposite of the guide's R10, which forbids the base from cloning; R10 assumes ONE base
serving a whole era, where pinning to a single PR's sha would be wrong. Here the base is
per-PR, so pinning it is not just safe but required.

Patches. `git apply --check` was run for both patches at 2dbc81623ef4 inside the
real image: `test_patch` and `test_patch + fix_patch` both apply clean. Neither
carries a binary hunk nor a committed build-output path, so no R19 patch-sanitising
helper is warranted here.

React version. `npm run react 16` is .travis.yml's default and resolves React
16.14.0, which test/_helpers/adapter.js routes (`is('^16.4.0-0')`) to
enzyme-adapter-react-16 -- the adapter whose ReactSixteenAdapter.js this PR repairs.
That adapter already imports `enzyme-shallow-equal` and declares it as a dependency
at the base commit, so the fix patch's package.json additions (which target the
14 / 16.1 / 16.2 / 16.3 adapters) need no re-bootstrap to take effect.

Test names. mocha's TAP reporter emits one `ok N <full title chain>` line per test,
so parse_log needs no indentation state machine and the shallow and mount runs of
the shared method suites stay distinct ("shallow .setContext(...)" vs
"(uses jsdom) mount .setContext(...)"). Three `describeIf` variants in
ShallowWrapper-spec.jsx do share one full title -- two skipped, one passing at
baseline -- and the test patch collapses them into a single unconditional
`describe`; the set arithmetic at the end of parse_log resolves that collision the
same way in every stage, so the transition stays visible.

These are title chains, not path-prefixed IDs, so report.py's
`_test_name_matches_files` cannot bind them to a file. Both matcher gates then
degrade permissively -- `_touched_by_test_patch` falls through to True
(report.py:168) and `_touched_by_fix_patch` to False (report.py:184) -- while the
precise `_authored_via_diff` content match still works, because a mocha title is a
literal string in the test file. The fix patch contains no test file, so the
cheating guard has nothing to catch here in any case.

Measured bucket shape (§8), captured from this config's own scripts in a node:14
container at 2dbc81623ef4:

    run        pass=1755  fail=0  skip=93
    test-run   pass=1756  fail=3  skip=93
    fix-run    pass=1759  fail=0  skip=93

Three f2p, no test that passes at the test stage and fails at the fix stage, and a
large p2p body: the healthy shape. (Counts are of distinct names; mocha's own
totals are 1779/1780+3/1783, the difference being the colliding titles above.)
"""

import re
from typing import Optional, Union

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

    def dependency(self) -> Union[str, "Image"]:
        return "node:14"

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

        # No apt layer, deliberately. node:14 is Debian buster, which is EOL: any
        # `apt-get update` here would 404 on deb.debian.org and kill the build with
        # exit 100 unless the sources were repointed at archive.debian.org (R11).
        # Nothing needs installing -- the node:14 image is built on
        # buildpack-deps:buster and already ships git 2.20.1, ca-certificates,
        # python 2.7 and build-essential, which is everything npm and node-gyp ask
        # for in this tree -- so the cheapest way to satisfy R11 is to have no apt
        # step at all.
        #
        # The clone below is deliberately written with the URL spelled out rather
        # than as "${REPO_URL}": DockerfileEnhancer._standardize_repo_fetch
        # (image.py:354-384) matches exactly that shape and rewrites this single
        # line into the canonical block --
        #     git clone "${REPO_URL}" -> WORKDIR -> git reset --hard ->
        #     git checkout ${BASE_COMMIT} -> {Image._HARDENING_BLOCK} -> CMD
        # -- so the rendered base carries the clone, the base-commit pin and the
        # history scrub, which is where the QC checklist (D11/D12/D13/D14) and the
        # rest of the tree expect them to live. A `"${REPO_URL}"` clone would be
        # skipped by that regex and would leave the base unpinned and unscrubbed.
        #
        # This base is per-PR (`base-pr-<n>`), not era-shared, so pinning it to
        # ${BASE_COMMIT} is correct. That is the opposite of the guide's R10, which
        # forbids the clone only because it assumes ONE base serving a whole era;
        # the in-tree deliverables this repo ships (see the servicecomb base in
        # output4) use the per-PR shape, so that is what is followed here.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" /home/{self.pr.repo}

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # R3: byte-identical in run.sh, test-run.sh and fix-run.sh. Built as a
        # local here rather than a module constant (R17) so the command and the
        # scripts that carry it stay in one place.
        run_tests = """
# mocha runs the *built* output, and the patches land in src/ and test/, so every
# stage rebuilds before it runs. `lerna run build` is offline: it is babel only.
npx lerna run build

# R14: mocha 3 is single-process -- there is no worker pool to disable and no
# --forceExit to add. R15: no `--` separator, the flags go straight to the mocha
# binary. `test/mocha.opts` is picked up automatically and supplies the jsdom and
# adapter --require lines. The TAP reporter prints one `ok N <full title chain>`
# line per test, which is exactly what parse_log reads.
npx mocha --recursive packages/enzyme-test-suite/build --reporter tap
"""

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
set -eo pipefail

# enzyme's own postinstall hook is `[ -n "${{TRAVIS-}}" ] || (npm link npm && lerna
# bootstrap)`. The bootstrap is run explicitly further down, via env.js, so TRAVIS
# is set here to suppress the hook rather than run it twice.
export TRAVIS=1
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# R12: a base commit that lives only on refs/pull/* is NOT in a plain clone
# (`git clone` fetches branches and tags, never PR refs). Fetch it on demand, then
# delete the temp refs so the hardening block's `rev-list --all == rev-list HEAD`
# assertion still holds. 2dbc81623ef4 is on master, so this is a no-op today.
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

# No `|| true` on any install below. A half-installed tree does not fail loudly at
# stage time -- it produces a mocha that dies at load and a stage that collects 0
# tests, which reads as a parse_log bug (§8). Failing the *build* instead makes the
# breakage impossible to miss.
npm install --no-audit --no-fund

# babel-preset-airbnb declares @babel/runtime as a PEER dependency, which npm 6
# does not install. Without it `babel-node ./env.js` (below) and every package
# build die with "Cannot find module '@babel/runtime/helpers/interopRequireDefault'".
# --no-save so package.json stays byte-identical for the stage-time `git apply`.
npm install --no-save --no-audit --no-fund "@babel/runtime@^7.12.5"

npm run clean-local-npm

# `npm run react 16` -> install-relevant-react.sh -> `babel-node ./env.js 16`:
# installs enzyme-adapter-react-16's React peer deps at the root and runs
# `lerna bootstrap --hoist='react*'`, which is what makes
# `require('enzyme-adapter-react-16')` resolve from the test suite. React 16 is
# .travis.yml's default; it resolves 16.14.0.
npm run react 16

# MUST come after the bootstrap above, which re-resolves it. packages/enzyme
# depends on `cheerio@^1.0.0-rc.3`; today that range resolves to cheerio 1.2.0,
# which requires node >= 20.18.1 and pulls in undici, whose `??=` syntax node 14
# cannot even parse. The suite then dies at load with
# "SyntaxError: Unexpected token '??='" and all three stages collect 0 tests.
# rc.3 is the release current at the base commit. --no-save keeps
# packages/enzyme/package.json pristine.
(cd packages/enzyme && npm install --no-save --no-audit --no-fund cheerio@1.0.0-rc.3)

# Warm the babel output so the first stage is not the one paying for it.
npx lerna run build

# R21: nothing above may leave a tracked file modified, or the stage-time
# `git apply` fails and the stage grades 0. env.js does rewrite two tracked
# package.json files -- the adapter's and the test suite's -- but it writes back
# byte-identical content, so the tree stays clean. Asserted rather than assumed.
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export TRAVIS=1
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
""".format(pr=self.pr)
                + run_tests,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export TRAVIS=1
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
""".format(pr=self.pr)
                + run_tests,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export TRAVIS=1
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/{pr.repo}
# R6: /home/fix.patch is where the agent's patch is bind-mounted at evaluation.
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
""".format(pr=self.pr)
                + run_tests,
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

        # Deliberately thin, and it must stay that way. The clone, the
        # ${BASE_COMMIT} pin, the history scrub and the CMD are all owned by the
        # base image; re-doing any of them here would duplicate a base
        # responsibility (QC item P9) and leave the base looking unpinned to anyone
        # auditing it on its own. dependency() returns an Image, so
        # DockerfileEnhancer returns this text verbatim -- what is written here is
        # exactly what gets built.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("enzymejs", "enzyme_2476_to_2476")
class ENZYME_2476_TO_2476(Instance):
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

        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        clean_log = ansi_escape.sub("", test_log)

        # mocha 3's TAP reporter (lib/reporters/tap.js) prints exactly one line per
        # test: `ok N <title>` on pass, `not ok N <title>` on failure, and
        # `ok N <title> # SKIP -` for a pending test. <title> is `fullTitle()` with
        # `#` stripped, so the trailing `# SKIP` marker is unambiguous and a title
        # can never contain one. Everything else in the log -- the `1..N` plan, the
        # `# tests/# pass/# fail` summary, and ~1000 lines of React console warnings
        # -- is left alone; none of it can be mistaken for a test name.
        re_ok = re.compile(r"^ok\s+\d+\s+(.+?)(\s+#\s+SKIP\b.*)?$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s+(.+)$")

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_not_ok.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_ok.match(line)
            if match:
                name = match.group(1).strip()
                if match.group(2):
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        # R2 -- the sets MUST be disjoint or TestResult raises. Failure wins, then
        # pass: three `describeIf` variants in ShallowWrapper-spec.jsx share one
        # full title, and at baseline two of them are pending while the third runs,
        # so the same name legitimately arrives as both skipped and passed.
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


# R26/§17.4 option 2: the dataset's row carries no `number_interval`, so
# Instance.create() computes the plain "enzymejs/enzyme" key. Registering it as an
# alias onto the range class keeps existing rows routable without editing the
# dataset, while the range key above stays available for rows that do carry one.
Instance.register("enzymejs", "enzyme")(ENZYME_2476_TO_2476)
