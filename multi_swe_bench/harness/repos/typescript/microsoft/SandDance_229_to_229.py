"""microsoft/SandDance -- era 1 of 1, PR interval 229 -> 229 (Node 10 / lerna 3 / mocha 7).

Era boundary. The dataset holds a single PR, so there is exactly one era. At PR
229's base (3e8719de70dd) `azure-pipelines.yml` pins `NodeTool@0 versionSpec:
'10.x'` and the root `package.json` pins `lerna ^3.20.2`, `mocha ^7.1.1`,
`typescript ^3.5` and `rollup 0.60`, so the toolchain is Node 10 on Debian
buster:

    SandDance_229_to_229.py   this file   1 PR   node:10-buster

Image shape. The base is tagged per PR (`base-pr-<N>`) and owns the clone, the
BASE_COMMIT pin, the history scrub and the proxy/CA trust; the PR layer is the
thin `COPY` + `RUN bash /home/prepare.sh` on top. This is the shape of
`repos/golang/ipfs/kubo.py`, which the Dockerfile QC checklists (D1-D18 for the
base, P1-P9 for the PR layer) grade against.

Registration. This era answers to `microsoft/SandDance_229_to_229`, which
Instance.create() (instance.py:41-49) builds only from a dataset row carrying
number_interval="SandDance_229_to_229". The shipped raw dataset
(microsoft__SandDance_raw_dataset.jsonl) carries no `number_interval`, so the
harness computes the plain key `microsoft/SandDance`; the alias registration at
the bottom of this file makes that key resolve here too (R26, HOW_TO §17.4
option 2 -- correct because this single era serves every row in the dataset).

Test surface. Two packages are graded, because the gold test patch touches
both:

  * `packages/chart-recommender` -- the only package whose `scripts.test` runs
    a real runner (`mocha`), driven here through mocha's JSON reporter.
  * `packages/sanddance-specs` -- `test/perf.js` and `test/demo.js` are
    executable scripts that no mocha suite loads (the package wires perf.js to
    its own `test2` script). The test patch rewrites both from
    `cloneVegaSpecWithData` onto the renamed `build` export, which is precisely
    what `fix.patch` provides, so they are the surface this PR actually
    changes. They carry no assertions, so each is graded on its exit status --
    a smoke check that the renamed entry point resolves and the pipeline runs.
    Grading only chart-recommender leaves this PR with no FAIL->PASS at all.

The root `npm test` is deliberately NOT used: it begins with `npm run eslint`,
which is `eslint --fix` and rewrites tracked sources in place (R21), and it
ends with `lerna run package`, which needs the npm registry at test time.

Test names. Mocha 7's JSON reporter does not report the spec file, so
`run-tests.sh` drives one spec file per invocation inside a
`###MOCHA_FILE_START:<repo-relative path>###` marker and parse_log() re-roots
every `fullTitle` under that path. Names therefore read
`packages/chart-recommender/test/recommend.js > Recommender <title>`, which is
the shape `report.py::_test_name_matches_files` matches against
`test_patch_files` (R20).

Patch sanitising. None. Both `test.patch` and `fix.patch` were checked with
`git apply --check` at 3e8719de70dd inside node:10-buster and apply cleanly, so
no `_sanitize_patch` helper is carried here (R19 has nothing to drop).

Where the FAIL->PASS comes from. This PR ("Vue.js component") renames
`cloneVegaSpecWithData` -> `build` and adds a `sanddance-vue` package; it never
touches `packages/chart-recommender/src`. So the one mocha test the patch adds,
`x/y: recommends scatter plot`, already passes at 3e8719de70dd --
`ScatterPlotRecommenderSummary` matched columns literally named `x`/`y` there
long before this PR. The transition instead comes from the two sanddance-specs
scripts, measured in-container at all three stages:

    packages/sanddance-specs/test/perf.js   PASS -> FAIL -> PASS
    packages/sanddance-specs/test/demo.js   NONE -> FAIL -> PASS

At the test stage both abort with
`SyntaxError: The requested module '.../dist/es6/index.js' does not provide an
export named 'build'`; the fix patch supplies that export and both run clean.
The transition is caused by the fix, not by weakening the test stage. The
cheating guard is satisfied by construction: `fix_patch_files` and
`test_patch_files` are disjoint, and the fix never edits either script.

The fourth file in the test patch, `docs/tests/v3/umd/sanddance-specs.html`, is
a browser page loading unpkg.com and is correctly left ungraded.
"""

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class SandDanceImageBase(Image):
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
        # azure-pipelines.yml at 3e8719de70dd pins NodeTool versionSpec '10.x'.
        # The non-slim tag is buildpack-deps based, so git, python2.7 and
        # build-essential are already present and no apt step is needed -- which
        # also means this era never has to touch buster's dead deb.debian.org
        # mirrors (R11). Any dependency added here in future must first rewrite
        # /etc/apt/sources.list to archive.debian.org.
        return "node:10-buster"

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

        # The base is tagged per PR (`base-pr-<N>`), not shared across the era,
        # so the clone belongs here -- this is the house shape used by
        # repos/golang/ipfs/kubo.py and 75 other configs. dependency() returns a
        # string, so DockerfileEnhancer rewrites the bare clone below into
        # `git clone "${REPO_URL}"` + WORKDIR + reset + checkout ${BASE_COMMIT}
        # + the history scrub + CMD, and prepends the syntax directive, build
        # ARGs, proxy/TLS ENV, OCI labels and the CA-cert symlink farm.
        # (HOW_TO's R10 warns against cloning in a base only because it assumes
        # a base *shared* by a whole era; a per-PR base tag cannot be mispinned.)
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # node:10-buster is EOL: deb.debian.org no longer carries buster, so a
        # plain `apt-get update` 404s and the build dies with exit 100. Repoint
        # at archive.debian.org first (R11). The toolchain itself (node, npm,
        # git, python, build-essential) already ships in this buildpack-deps
        # based tag; git and ca-certificates are re-asserted here explicitly.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/buster-updates/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class SandDanceImageDefault(Image):
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
        return SandDanceImageBase(self.pr, self.config)

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
# The base image already cloned, pinned HEAD to this sha and scrubbed the
# history, so the object is present and `origin` is gone. `git cat-file -e`
# short-circuits; the fetch is a self-cleaning fallback that never runs here
# (R12 -- it matters only for a base commit living solely on refs/pull/*).
git cat-file -e {pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

npm install --no-audit --no-fund --loglevel=error || true

# The fix patch moves the bundler from rollup 0.60 + rollup-plugin-* onto
# rollup ^1.32 + @rollup/plugin-*, and adds a Vue package. Those modules must
# exist before `rollup -c` runs at the fix stage, and the stages have no
# network. Install them at build time with --no-save so neither package.json
# nor any package-lock.json is rewritten (R21/R22) -- the dataset's patches
# touch both and git apply would reject a locally edited manifest.
npm install --no-save --no-audit --no-fund --loglevel=error \\
    "@rollup/plugin-buble@^0.21.3" \\
    "@rollup/plugin-commonjs@^12.0.0" \\
    "@rollup/plugin-json@^4.0.3" \\
    "@rollup/plugin-node-resolve@^8.0.0" \\
    "@rollup/plugin-typescript@^4.1.1" \\
    "rollup@^1.32.1" \\
    "rollup-plugin-vue@^5.0.1" \\
    "vue-template-compiler@^2.6.11" \\
    "esm@^3.2.25" || true

# Bootstrap only chart-recommender and its five transitive local packages
# (sanddance, sanddance-specs, vega-deck.gl, chart-types, search-expression).
# A full `lerna bootstrap` also installs sanddance-app and the explorer, which
# the mocha suite never loads.
./node_modules/.bin/lerna bootstrap --scope @msrvida/chart-recommender --include-dependencies || true

bash /home/run-tests.sh || true

# R21: prove that neither the install nor the build rewrote a tracked file.
# `packages/sanddance` runs scripts/version.js during its build; it writes into
# dist/, but a regression there would silently break every later git apply.
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
set -eo pipefail

# Compiled output, not sources, is what the suite loads: recommend.js requires
# ../dist/es5 and @msrvida/sanddance/dist/umd/sanddance. Every stage therefore
# rebuilds before running, so a patch to sanddance/sanddance-specs sources is
# actually visible to the tests. A build failure is fatal on purpose -- running
# mocha against a stale dist/ would grade the previous build.
cd /home/{pr.repo}
./node_modules/.bin/lerna run build --scope @msrvida/chart-recommender --include-dependencies

# Mocha 7's JSON reporter omits the spec file, so drive one file per invocation
# and name the block after it; parse_log() re-roots the titles under this path
# so report.py can tie each test back to its file (R20). `|| true` guards the
# loop only -- mocha writes its JSON before exiting non-zero, so no output is
# swallowed, and without it `set -e` would drop every later spec file.
#
# --timeout 120000 is load-bearing, not padding. Mocha's 2000ms default is not
# survivable here: `longitude/latitude: recommends scatter plot` parses the
# larger docs/sample-data/demovote.tsv and measures ~700ms on an idle host but
# 2247-2875ms on a busy one. Exceeding it produced a PASS at the test stage and
# a FAIL at run and fix, which trips Report.check() step 2 ("no new failures")
# and invalidates the whole instance. The wall-clock cost is nil -- the timeout
# only caps a hang -- and raising it changes no test name, so the three stages
# stay byte-comparable (R3).
cd /home/{pr.repo}/packages/chart-recommender
for spec in test/*.js; do
    [ -e "$spec" ] || continue
    echo "###MOCHA_FILE_START:packages/chart-recommender/$spec###"
    /home/{pr.repo}/node_modules/.bin/mocha --reporter json --timeout 120000 "$spec" || true
    echo "###MOCHA_FILE_END###"
done

# packages/sanddance-specs ships executable scripts under test/ that no mocha
# suite ever loads -- the package wires perf.js to its own `test2` script. The
# gold test patch rewrites both onto the renamed `build` export, so they are
# exactly the surface this PR changes and they must be graded. They carry no
# assertions, so the pass/fail signal is the exit status: an unresolved import
# aborts the script. `esm` comes from the root node_modules (prepare.sh
# installs it with --no-save) because the fix patch moves it off this
# package's own devDependencies.
cd /home/{pr.repo}/packages/sanddance-specs
for spec in test/*.js; do
    [ -e "$spec" ] || continue
    echo "###NODE_FILE_START:packages/sanddance-specs/$spec###"
    if node -r /home/{pr.repo}/node_modules/esm "$spec"; then
        echo "###NODE_RESULT:PASS###"
    else
        echo "###NODE_RESULT:FAIL###"
    fi
    echo "###NODE_FILE_END###"
done

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

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

        # Thin layer only. The clone, the BASE_COMMIT pin, the history scrub and
        # the proxy/CA trust are all earned in the base image above; re-doing any
        # of them here would duplicate or undo a guarantee that was already
        # established. dependency() returns an Image, so DockerfileEnhancer hands
        # this text back verbatim (R9) -- what is written here is what is built.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("microsoft", "SandDance_229_to_229")
class SANDDANCE_229_TO_229(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SandDanceImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1B\[[0-9;]*m", "", test_log)

        re_block = re.compile(
            r"###MOCHA_FILE_START:(?P<file>[^#\n]+)###\n"
            r"(?P<body>.*?)"
            r"###MOCHA_FILE_END###",
            re.DOTALL,
        )

        for block in re_block.finditer(clean_log):
            spec_file = block.group("file").strip()
            body = block.group("body")

            # recommend.js prints the sample-file list while the suite is being
            # defined, so the reporter's document does not start at byte 0. It
            # is the last line that begins a top-level object: every brace
            # inside the JSON is indented.
            start = body.rfind("\n{")
            if start != -1:
                start += 1
            elif body.lstrip().startswith("{"):
                start = body.index("{")
            else:
                # mocha never reached the reporter (a require-time throw, a
                # missing dist/, a syntax error in the spec). Nothing from this
                # file is observable; leave every bucket untouched so the tests
                # grade NONE rather than inventing a status for them.
                continue

            depth = 0
            end = -1
            for i, ch in enumerate(body[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end == -1:
                continue

            try:
                data = json.loads(body[start:end])
            except ValueError:
                continue

            for key, bucket in (
                ("passes", passed_tests),
                ("failures", failed_tests),
                ("pending", skipped_tests),
            ):
                for test in data.get(key) or []:
                    title = (test.get("fullTitle") or test.get("title") or "").strip()
                    if title:
                        bucket.add(f"{spec_file} > {title}")

        # The sanddance-specs scripts report a single exit status each, named by
        # their repo-relative path so report.py can tie them back to the gold
        # test patch that rewrote them (R20).
        re_node_block = re.compile(
            r"###NODE_FILE_START:(?P<file>[^#\n]+)###\n"
            r"(?P<body>.*?)"
            r"###NODE_FILE_END###",
            re.DOTALL,
        )

        for block in re_node_block.finditer(clean_log):
            spec_file = block.group("file").strip()
            body = block.group("body")
            if "###NODE_RESULT:PASS###" in body:
                passed_tests.add(spec_file)
            elif "###NODE_RESULT:FAIL###" in body:
                failed_tests.add(spec_file)
            # Neither marker means the stage died before the script returned;
            # leave it unrecorded so it grades NONE rather than inventing one.

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


# R26: the shipped raw dataset carries no `number_interval`, so Instance.create()
# computes the plain key `microsoft/SandDance`. One era serves every row, so
# aliasing it here is safe and keeps the range-file layout (R24) intact.
Instance.register("microsoft", "SandDance")(SANDDANCE_229_TO_229)
