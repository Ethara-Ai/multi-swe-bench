"""sjbarag/brs harness config.

Dataset shape: ONE pull request (#640, 2021-04). By rule 4's table that puts it
in the single-PR row, so the base is per-PR (`base-pr-<N>`) and carries the
COMPLETE history scrub, and the `pr-<N>` layer stays thin.

Toolchain: Node 14 on bullseye, Yarn 1 (classic, `yarn.lock` in the repo),
TypeScript 3 compiled to `lib/` by `yarn build`, Jest 26 with the minimal
`{"testEnvironment": "node"}` block in package.json - there is no jest.config
file. Node 14 is not a guess: `.github/workflows/pull-request.yaml` at this base
commit runs the matrix `node_version: [10, 12, 14]`, so 14 is the newest version
the project actually tested against.

Image layout - the `mvdan/sh.py` shape
--------------------------------------
  base-pr-<N>   clone, check out this PR's base commit, install and build, then
                the COMPLETE history scrub: `Image._HARDENING_BLOCK` verbatim,
                gc and repack and all four integrity asserts.

  pr-<N>        deliberately thin: stage the patches and run scripts, run
                prepare.sh, CMD. NO scrub block at all.

The scrub lives in the base and only in the base. It opens with
`git checkout --detach "${BASE_COMMIT}"`, so it can only run where BASE_COMMIT is
a real value - and it is real here because `dependency()` returns a str, which is
what makes build_dataset.py:625-629 pass REPO_URL and BASE_COMMIT as build args.
An Image-dependency layer receives no build args at all, so the same block in
`pr-<N>` would be scrubbing against a value handed to it by hand.

That is also why the tag is `base-pr-<N>` rather than a shared era tag: the prune
needs a pinned HEAD, and pinning a SHARED base would fix it to whichever PR built
it first. With a single-PR dataset this costs nothing - two images either way.

Why `yarn install` belongs in the base
--------------------------------------
Rule 5 wants the PR layer thin, and the install is the expensive step (~45 s).
It is also the one step that must not be repeated per stage: the run scripts are
executed three times inside the SAME container, and re-resolving the dependency
tree each time would make the run depend on the network long after build time.
`node_modules/` and `lib/` are both gitignored, so nothing the install writes can
dirty the tree the hardening asserts inspect.

Why the log is JSON and not Jest's `--verbose` tick marks
---------------------------------------------------------
The usual `^\\s*✓\\s+(.+)$` scrape keeps only the LEAF test title, and in this
repo leaf titles are not unique: 1474 assertions share 1451 distinct `fullName`
values across the suite. Scraping tick marks would silently merge ~23 unrelated
tests into shared ids - exactly the collapsed-suite failure mode that also
produces false `fix_patch_authored_candidates` hits.

So the runners ask Jest for its machine-readable report (`--json --outputFile`)
and fence it between two markers, and `parse_log` reads that. Ids are
`<path relative to the repo root>::<fullName>`, with `#2`, `#3`, ... appended for
the 15 (file, fullName) pairs that genuinely repeat inside one file. That yields
1474 distinct ids for 1474 assertions - no collapsing at all.

Measured in node:14-bullseye at base commit 6dd52c1c (2026-08-25), full suite,
~35 s per stage:

    baseline (no patches)   1468 passed, 6 skipped/todo, 0 failed
    + test.patch            1443 passed, 6 skipped/todo, 25 failed
    + test.patch + fix.patch 1468 passed, 6 skipped/todo, 0 failed

so F2P is 25 and P2P is 1443, and the baseline and post-fix sets are identical.
"""

import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Where Jest writes its machine-readable report, and the fence `parse_log` looks
# for. The fence matters because the harness captures stdout AND stderr, so the
# report is surrounded by Jest's own progress output.
_REPORT_PATH = "/home/jest-report.json"
_JSON_BEGIN = "-----BEGIN_JEST_JSON-----"
_JSON_END = "-----END_JEST_JSON-----"

# ONE definition, used by all three run scripts, so the baseline, test-patch and
# fix-patch stages can never drift apart. `--ci` keeps Jest from writing new
# snapshots on the fly - this repo has 173 of them, and silently creating one
# would turn a genuine failure into a pass.
_TEST_CMD = f"./node_modules/.bin/jest --ci --json --outputFile={_REPORT_PATH}"


def _runner(repo: str, apply_block: str) -> str:
    """Body shared by run.sh / test-run.sh / fix-run.sh.

    No `set -e`: a failing Jest run is the expected outcome of the test-patch
    stage, and the report still has to be printed afterwards. Failures that are
    NOT expected - a patch that will not apply - exit loudly instead.

    `lib/` and `types/` are removed before every build so a broken `tsc` cannot
    leave a stale, previously-good `lib/` behind for Jest to test against. If the
    build produces nothing, every suite fails to load, which is the honest
    signal.
    """
    return """#!/bin/bash
set -uo pipefail

cd /home/{repo}

{apply_block}rm -f {report}
rm -rf lib types

yarn build || echo "BUILD_FAILED"

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


class BrsImageBase(Image):
    """Per-PR base for sjbarag/brs.

    Pinned to this PR's own BASE_COMMIT and carrying the COMPLETE history scrub -
    gc, repack and all four integrity asserts - so `pr-<N>` has no scrub at all.
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

        # The COMPLETE scrub - gc, repack and all four integrity asserts - lives
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
        # end of the file - after `yarn install`, and duplicating a prune that
        # has already run.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                "ARG TARGETARCH\n"
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
            # Yarn 1 prints a progress bar and asks questions on some failures;
            # neither is wanted in a build log. YARN_CACHE_FOLDER is pinned so
            # the cache lands somewhere writable regardless of HOME.
            'ENV YARN_CACHE_FOLDER="/usr/local/share/.cache/yarn" \\\n'
            '    npm_config_loglevel="warn"',
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            # Both are done here, not in pr-<N>: the install is the expensive,
            # network-dependent step and the run scripts must not repeat it.
            # `--frozen-lockfile` makes a drifting yarn.lock a build failure
            # rather than a silently different dependency tree.
            "RUN yarn install --frozen-lockfile --non-interactive",
            "RUN yarn build",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class BrsImageDefault(Image):
    """Per-PR image: stage the patches and run scripts, assert the checkout is
    exactly this PR's base commit, and leave it there.

    Carries no history scrub - `base-pr-<N>` already ran the complete one.
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
        return BrsImageBase(self.pr, self._config)

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

# The base already cloned, checked out {sha}, installed and built. This layer
# only proves that state is what it claims to be - deliberately no `git clean
# -fdx`, which would delete the node_modules/ the base spent ~45 s producing.
git reset --hard
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"
test -d node_modules
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
        # scrub -- {tag} is pinned to this PR's base commit and has already run
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


@Instance.register("sjbarag", "brs")
class Brs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BrsImageDefault(self.pr, self._config)

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
        when the same (file, fullName) pair repeats inside one file. Both parts
        are needed: 23 of this repo's 1474 assertions share a `fullName` with an
        assertion in a different file, and another 15 repeat inside their own
        file. Anything less specific merges unrelated tests into one id.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        start = clean_log.find(_JSON_BEGIN)
        end = clean_log.rfind(_JSON_END)
        if start == -1 or end <= start:
            # No report at all: the stage never got as far as writing one.
            # Reporting nothing is correct - inventing results from Jest's
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
                # Without this it would simply vanish from every set, which reads
                # as "those tests were removed" rather than "they blew up".
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
