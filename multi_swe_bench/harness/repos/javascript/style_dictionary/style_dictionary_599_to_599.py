"""style-dictionary/style-dictionary harness config for the #599 era.

Dataset shape: ONE pull request (#599, 2021-05, "feat(compose): Add Jetpack
Compose format"). By rule 4's table that is the single-PR row, so the base is
per-PR (`base-pr-<N>`) and carries the COMPLETE history scrub, and the `pr-<N>`
layer stays thin.

Why a separate era file at all
------------------------------
`style_dictionary_939_to_429.py` already registers a range that #599 falls inside
numerically, but it cannot build this commit: it uses `node:22` and installs with
`npm install --ignore-scripts`. At base commit 0f65865f the tree is
style-dictionary 3.0.0-rc.8 (May 2021), whose devDependencies include
`node-sass@^4.14.1` -- a native addon with no Node 22 binding, and one that
`--ignore-scripts` never builds at all. `__integration__/scss.test.js` does
`require('node-sass')`, so that suite would fail in every stage. Retuning that
file would also change the toolchain for every other PR in the 429-939 range it
was written for, which rule 0 puts out of bounds. Hence a new, self-contained
era file, reached by stamping `number_interval` in run_pipeline.sh.

Toolchain: Node 14 on bullseye, npm 6 with the repo's lockfileVersion-1
`package-lock.json`, Jest 25. Node 14 is not a guess -- `.github/workflows/test.yml`
at this base commit runs the matrix `node-version: [12.x, 14.x, 15.x]` with
`npm ci`, and 14 is the newest version for which node-sass 4.14.1 publishes a
prebuilt binding (ABI 83).

Why there is an apt step, and why it installs exactly one package
-----------------------------------------------------------------
`node:14-bullseye` is a full buildpack-deps image, so git, curl, ca-certificates,
gcc, g++ and make are all already there -- verified with `command -v` inside the
built image, not assumed. The single thing it does NOT ship is a Python 2
interpreter, and that is needed on **arm64**:

    node-sass 4.14.1 release assets: linux-x64-83_binding.node,
                                     linux_musl-x64-83_binding.node
                                     ... and no arm64 asset at all.

So on arm64 `npm ci` cannot download a binding and falls back to building libsass
from source through node-gyp. node-sass 4.14.1 depends on `node-gyp ^3.8.0`,
which predates Python 3 support and refuses to run under it, and the image has
only `python3`. Without `python2` the arm64 layer dies in `npm ci`; with it, the
same recipe builds on both platforms.

It is installed unconditionally rather than behind `TARGETARCH` so the two
architectures build from a byte-identical Dockerfile. On amd64 the prebuilt
binding is used and node-gyp never runs, so the package costs ~4 MB and nothing
else.

Caveat worth knowing: Debian 11 leaves LTS on 2026-08-31. `apt-get update` will
keep working until bullseye moves to archive.debian.org, after which this one
line needs `deb.debian.org` swapped for the archive host. It is the only reason
this image touches the network for packages at all.

Image layout -- the `mvdan/sh.py` shape
---------------------------------------
  base-pr-<N>   clone, check out this PR's base commit, install, then the
                COMPLETE history scrub: `Image._HARDENING_BLOCK` verbatim, gc and
                repack and all four integrity asserts.

  pr-<N>        deliberately thin: stage the patches and run scripts, run
                prepare.sh, CMD. NO scrub block at all.

The scrub lives in the base and only in the base. It opens with
`git checkout --detach "${BASE_COMMIT}"`, so it can only run where BASE_COMMIT is
a real value -- and it is real here because `dependency()` returns a str, which is
what makes build_dataset.py:625-629 pass REPO_URL and BASE_COMMIT as build args.
An Image-dependency layer receives no build args at all, so the same block in
`pr-<N>` would be scrubbing against a value handed to it by hand.

That is also why the tag is `base-pr-<N>` rather than a shared era tag: the prune
needs a pinned HEAD, and pinning a SHARED base would fix it to whichever PR built
it first. With a single-PR dataset this costs nothing -- two images either way.

Why `npm ci` belongs in the base
--------------------------------
Rule 5 wants the PR layer thin, and the install is the expensive, network-bound
step. It is also the one step that must not be repeated per stage: the harness
starts a SEPARATE container from the same image for each of run / test / fix
(build_dataset.py:754 -> docker_util.run), so an install inside a run script
would be paid three times over and would make a graded run depend on the network
long after build time. Baking it into the image instead means all three stages
start from a byte-identical dependency tree. Neither patch touches
`package.json` or `package-lock.json`, so there is nothing for a later stage to
reinstall. `node_modules/` is gitignored, so nothing the install writes can dirty
the tree the hardening asserts inspect.

`npm ci` rather than `npm install` because it is what CI runs and because it
fails on a lockfile that does not match `package.json`, instead of silently
resolving a different dependency tree.

Test command
------------
`--runInBand` is mandatory, not a performance choice. Every file under
`__integration__/` builds into the single shared `__integration__/build/`
directory named by `__integration__/_constants.js`, and each one ends with
`afterAll(() => fs.emptyDirSync(buildPath))`. Run in parallel workers those
suites delete each other's output mid-read. The repo's own `npm test` script
passes `--runInBand` for the same reason.

`--ci` is not optional either: it stops Jest writing a missing snapshot on the
fly and reporting it as a pass. This PR is graded almost entirely through
snapshots, so a silently-created snapshot would turn the whole f2p signal green.

Coverage, `tsd` and eslint -- the other three things the repo's `npm test` runs --
are deliberately left out. None of them reports per-test results, and the fix
patch adds no types, so they would only add failure modes.

Why the log is JSON and not Jest's `--verbose` tick marks
---------------------------------------------------------
A `^\\s*[check]\\s+(.+)$` scrape keeps only the LEAF test title, and leaf titles in
this repo are emphatically not unique -- `__tests__/formats/all.test.js` alone
generates `should match <key> snapshot` for every entry in `lib/common/formats.js`
inside one shared `describe('all')`, and near-identical `should match snapshot`
titles recur across the `__integration__` suites. So the runners ask Jest for its
machine-readable report (`--json --outputFile`) and fence it between two markers,
and `parse_log` reads that. Ids are `<path relative to the repo root>::<fullName>`,
with `#2`, `#3`, ... appended if a (file, fullName) pair ever repeats inside one
file.

Putting the path first is safe here (see the `<tool>::<path>` caveat): report.py's
cheating guard treats the head of an id as a file path, and this fix patch touches
only `docs/`, `examples/`, `lib/` and `scripts/` -- no test file -- so no id head
can match a fix-patch file, `fix_patch_files & test_patch_files` is empty, and
guard #5 correctly stays out of the way.

Expected classification
-----------------------
F2P comes from `__tests__/common/transforms.test.js`: the test patch adds eight
cases against `color/composeColor`, `size/compose/remToSp`, `size/compose/em` and
`size/compose/remToDp`, which do not exist until the fix patch adds them, so they
raise TypeError in the test stage and pass in the fix stage.

N2P comes from `__integration__/compose.test.js`, which is new in the test patch
and cannot even load without the `compose` transformGroup, plus its snapshot file.

One known wrinkle, recorded so it is not read as a bug: `__tests__/formats/all.test.js`
loops over `_.keys(formats)`, so the fix patch's new `compose/object` format makes
two ids appear that no stage before it had --

    __tests__/formats/all.test.js::formats all should match compose/object snapshot
    __tests__/formats/all.test.js::formats all should return compose/object as a string

Both are run=NONE, test=NONE, fix=PASS. `_touched_by_test_patch` cannot see them:
the test patch edits `__tests__/formats/__snapshots__/all.test.js.snap`, not
`all.test.js`, so the file fallback misses, and `_appears_in_added` wants the title
delimited by quotes, which the snapshot key `exports[`... snapshot 1`]` is not.
They therefore land in `fix_patch_authored_candidates` rather than n2p. That is a
false positive of the detector, not collapsed ids: the tests are authored by the
repo's own generic loop and merely enumerated by the fix patch. Excluding
`all.test.js` to make the number zero would throw away ~40 genuine p2p cases, so
the count is left honest and explained here instead.
"""

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Where Jest writes its machine-readable report, and the fence `parse_log` looks
# for. The fence matters because the harness captures stdout AND stderr, so the
# report always arrives surrounded by Jest's own progress output.
_REPORT_PATH = "/home/jest-report.json"
_JSON_BEGIN = "-----BEGIN_JEST_JSON-----"
_JSON_END = "-----END_JEST_JSON-----"

# ONE definition, used by all three run scripts, so the baseline, test-patch and
# fix-patch stages can never drift apart. See the module docstring for why
# --runInBand and --ci are both required rather than merely nice to have.
_TEST_CMD = (
    f"./node_modules/.bin/jest --ci --runInBand --json --outputFile={_REPORT_PATH}"
)


def _runner(repo: str, apply_block: str) -> str:
    """Body shared by run.sh / test-run.sh / fix-run.sh.

    No `set -e`: a failing Jest run is the expected outcome of the test-patch
    stage, and the report still has to be printed afterwards. Failures that are
    NOT expected -- a patch that will not apply -- exit loudly instead.

    Both scratch directories are cleared first. They are gitignored (`build/` and
    `__tests__/__output` in .gitignore), so removing them cannot dirty the tree.
    The graded stages each get a fresh container, so this is belt-and-braces
    there; it matters when the same container runs a script more than once (the
    session/human-mode path), where a suite that threw before its
    `afterAll(emptyDirSync)` would otherwise leave an artifact behind for the
    next invocation to read and mistake for its own output.
    """
    return """#!/bin/bash
set -uo pipefail

cd /home/{repo}

{apply_block}rm -f {report}
rm -rf __integration__/build __tests__/__output

{test_cmd} || true

echo "{begin}"
if [ -f {report} ]; then
    cat {report}
fi
echo
echo "{end}"
""".format(
        repo=repo,
        apply_block=apply_block,
        report=_REPORT_PATH,
        test_cmd=_TEST_CMD,
        begin=_JSON_BEGIN,
        end=_JSON_END,
    )


def _apply(patch_path: str) -> str:
    """Apply one patch, or stop the stage.

    A patch that does not apply must not be allowed to fall through to a green
    run: the test-patch stage would then simply reproduce the baseline and F2P
    would come out empty rather than wrong-looking. Written with `if !` rather
    than `... || { ...; }` so the template carries no literal braces.
    """
    return """if ! git apply --whitespace=nowarn {patch}; then
    if ! git apply --3way --whitespace=nowarn {patch}; then
        echo "PATCH_APPLY_FAILED: {patch}"
        exit 1
    fi
fi
""".format(patch=patch_path)


class StyleDictionary599ImageBase(Image):
    """Per-PR base for style-dictionary/style-dictionary #599.

    Pinned to this PR's own BASE_COMMIT and carrying the COMPLETE history scrub --
    gc, repack and all four integrity asserts -- so `pr-<N>` has no scrub at all.
    See the module docstring for why the tag is `base-pr-<N>` and not a shared
    era name.
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
        return "node:14-bullseye"

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        # The COMPLETE scrub -- gc, repack and all four integrity asserts -- lives
        # here and only here. `Image._HARDENING_BLOCK` is used verbatim rather
        # than a hand-rolled variant so the asserts can never quietly diverge
        # from the harness's own definition; it already carries the submodule
        # pass as its second RUN.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file:
        #
        #     if cls.SYNTAX_DIRECTIVE in raw: return raw     (image.py:316-317)
        #
        # and the directive has to stay. Dropping it to re-enable the enhancer
        # would let _standardize_repo_fetch() rewrite the clone and
        # _inject_final_sanitize() append a SECOND hardening block at the very
        # end of the file -- after `npm ci`, duplicating a prune that has already
        # run.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            # npm 6 prints a progress bar and a funding banner that are pure
            # noise in a build log, and node-sass's installer honours the same
            # loglevel. CI=true also keeps npm from ever prompting.
            # npm_config_python names the interpreter node-gyp must use. Only
            # the arm64 build actually invokes node-gyp (no prebuilt node-sass
            # binding for that arch), but the variable is harmless on amd64 and
            # keeps one recipe for both.
            'ENV CI="true" \\\n'
            '    npm_config_loglevel="warn" \\\n'
            '    npm_config_progress="false" \\\n'
            '    npm_config_fund="false" \\\n'
            '    npm_config_audit="false" \\\n'
            '    npm_config_python="/usr/bin/python2"',
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            # One package, and only because arm64 needs it: node-sass 4.14.1
            # ships no arm64 binding, so `npm ci` below compiles libsass through
            # node-gyp 3.8, which refuses to run under python3. git, curl,
            # ca-certificates, gcc, g++ and make are already in this image --
            # verified inside it, not assumed -- so none of them is listed here.
            # See the module docstring for the Debian 11 EOL caveat.
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        python2 \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            # `git status` compares cached stat info before it compares content,
            # so a clone whose DEFAULT-branch checkout converted a file (CRLF via
            # a .gitattributes that the base commit does not have) can look clean
            # here and then report dirty in the delivered image, where Docker
            # layering has changed dev/ino and forced the content compare.
            # Re-materialising the tree and asserting afterwards removes that
            # class of surprise entirely rather than relying on stat-cache luck.
            "RUN set -eux; \\\n"
            "    git rm --cached -r -q .; \\\n"
            "    git reset --hard; \\\n"
            '    test -z "$(git status --porcelain)"',
            # Done here, not in pr-<N>: this is the expensive, network-bound step
            # and the run scripts must not repeat it. `npm ci` fails on a
            # lockfile that disagrees with package.json instead of quietly
            # resolving something else, and it is what .github/workflows/test.yml
            # runs. node-sass's postinstall downloads its prebuilt binding here.
            "RUN npm ci",
            # Prove the native addon that __integration__/scss.test.js requires
            # actually loaded. Without this a failed binding download would only
            # surface much later, as a whole suite failing identically in all
            # three stages -- which reads like a broken repo, not a broken image.
            'RUN node -e "require(\'node-sass\'); console.log(\'node-sass ok\')"',
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class StyleDictionary599ImageDefault(Image):
    """Per-PR image: stage the patches and run scripts, assert the checkout is
    exactly this PR's base commit, and leave it there.

    Carries no history scrub -- `base-pr-<N>` already ran the complete one.
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

    def dependency(self) -> Image:
        return StyleDictionary599ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e
cd /home/{repo}
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: /home/{repo} is not a git repository" >&2
    exit 1
fi
# Force a content compare. Without the refresh, status is allowed to trust the
# index's cached size/mtime/dev/ino and skip reading the file, so this check can
# pass on a worktree whose bytes do not match their blob.
git update-index -q --really-refresh || true
if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: working tree is dirty:" >&2
    git status --porcelain >&2
    exit 1
fi
echo "check_git_changes: clean at $(git rev-parse HEAD)"
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}

# The base already cloned, checked out {sha} and installed. This layer only
# proves that state is what it claims to be -- deliberately no `git clean -fdx`,
# which would delete the node_modules/ the base spent minutes producing.
git reset --hard
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"
test -d node_modules
test -x node_modules/.bin/jest
""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(".", "run.sh", _runner(self.pr.repo, "")),
            File(
                ".",
                "test-run.sh",
                _runner(self.pr.repo, _apply("/home/test.patch")),
            ),
            File(
                ".",
                "fix-run.sh",
                _runner(
                    self.pr.repo,
                    _apply("/home/test.patch") + _apply("/home/fix.patch"),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        file_names = " ".join(file.name for file in self.files())
        copy_command = f"COPY {file_names} /home/"

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- the base is pinned to this PR's base commit and has already run
        # the full scrub (gc, repack, all four asserts), so there is nothing left
        # to prune here.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

CMD ["/bin/bash"]
"""


@Instance.register("style-dictionary", "style_dictionary_599_to_599")
class StyleDictionary599To599(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return StyleDictionary599ImageDefault(self.pr, self._config)

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
        """Read Jest's `--json` report out of the fenced region of the log.

        Ids are `<repo-relative path>::<fullName>`, with `#2`, `#3`, ... appended
        when the same (file, fullName) pair repeats inside one file. The path is
        not decoration: `__tests__/formats/all.test.js` produces titles such as
        `formats all should match scss/variables snapshot` while
        `__tests__/formats/scssVariables.test.js` has its own near-identical
        titles, and every `__integration__` suite ends in `should match snapshot`.
        Anything less specific merges unrelated tests into one id.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        start = clean_log.find(_JSON_BEGIN)
        end = clean_log.rfind(_JSON_END)
        if start == -1 or end <= start:
            # No report at all: the stage never got as far as writing one.
            # Reporting nothing is correct -- inventing results from Jest's
            # human-readable output would collapse ids (see the docstring).
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        payload = clean_log[start + len(_JSON_BEGIN) : end].strip()
        try:
            report = _json.loads(payload)
        except Exception:
            report = {}

        marker = f"/{self.pr.repo}/"
        for suite in report.get("testResults") or []:
            path = suite.get("name") or suite.get("testFilePath") or ""
            path = path.replace("\\", "/")
            idx = path.rfind(marker)
            rel = path[idx + len(marker) :] if idx != -1 else path

            assertions = suite.get("assertionResults") or []
            if not assertions:
                # A suite that threw while loading reports no assertions at all.
                # `__integration__/compose.test.js` does exactly this in the
                # test-patch stage: its `StyleDictionary.extend(...)` runs at
                # describe time and there is no `compose` transformGroup yet.
                # Without this line the suite would simply vanish from every set,
                # which reads as "those tests were removed" rather than "they
                # blew up".
                failed_tests.add(f"{rel}::<suite did not run>")
                continue

            seen: dict[str, int] = {}
            for assertion in assertions:
                full_name = assertion.get("fullName") or " ".join(
                    list(assertion.get("ancestorTitles") or [])
                    + [assertion.get("title") or ""]
                ).strip()
                name = f"{rel}::{full_name}"
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name}#{seen[name]}"

                status = assertion.get("status")
                if status == "passed":
                    passed_tests.add(name)
                elif status == "failed":
                    failed_tests.add(name)
                else:
                    # pending / todo / disabled
                    skipped_tests.add(name)

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
