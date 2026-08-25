import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Node 16 is the newest line in this era's CI matrix (.github/workflows/ci.yml
# tests [8, 12, 14, 16] at the base commit, 2021-10-11), and 16.11.1 is the
# 16.x release current on that date. Pinned to the patch version so a rebuild
# reproduces the same runtime. The full Debian variant (not -slim, not -alpine)
# ships git, ca-certificates, bash and a C toolchain, so no apt layer is needed
# - and the official image publishes linux/amd64 AND linux/arm64, which is what
# makes the multi-arch manifest possible. The monorepo has no native modules
# (no binding.gyp anywhere in packages/*, verified), so arm64 is a pure
# reinstall, not a compile.
NODE_IMAGE = "node:16.11.1"

# The root CI job runs the whole monorepo suite:
#   cross-env NODE_ENV=test nyc mocha --timeout 30000 packages/*/test{,/*}.js
# This command is the same thing minus two substitutions, each with a reason:
#
#   * `NODE_ENV=test` as a plain bash prefix instead of cross-env - the stage
#     scripts are bash on Linux, cross-env exists only to make that prefix work
#     on Windows shells.
#   * `--reporter tap` - mocha's default spec reporter marks passing tests with
#     a U+2713 tick that the build-time ASCII filter strips, leaving passes
#     indistinguishable from plain log lines. TAP is pure ASCII by design:
#     `ok N <name>` / `not ok N <name>` / `# SKIP`, one line per test, which is
#     exactly what parse_log wants.
#
# The glob stays single-quoted so bash does not brace-expand it; mocha resolves
# it internally, same as npm's `sh` (which cannot brace-expand) does in CI.
# Running the FULL suite, not just istanbul-lib-source-maps, is deliberate:
# every other package's tests pass in all three stages and become p2p signal.
TEST_CMD = (
    "NODE_ENV=test npx nyc mocha --reporter tap --timeout 30000"
    " 'packages/*/test{,/*}.js'"
)

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR). The harness streams `docker buildx` output through
# `subprocess` with `text=True` and no explicit encoding, so a Windows host
# decodes it with cp1252 - and any UTF-8 byte outside that map aborts the
# build. Runtime logs are decoded as UTF-8 explicitly, so only build-time
# commands are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# Declared ONCE, in the base image; Docker propagates ENV to the PR image.
# CI=true keeps mocha/npm non-interactive; the npm_config_* pair kills the
# progress bar and color codes that would otherwise fight the ASCII filter.
ENCODING_ENV = """ENV CI=true \\
    NO_COLOR=1 \\
    npm_config_color=false \\
    npm_config_progress=false"""


class ImageBase(Image):
    """Per-PR base: OS + Node toolchain + the repo frozen at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is
    inside it (QC item P1). A single shared `base` tag cannot make that promise
    - the first PR to build it freezes it, and every later PR silently inherits
    the wrong commit while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form: that exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite supplies the REPO_URL/BASE_COMMIT clone, the
    checkout, the history-sanitising scrub with its four integrity asserts, and
    the final CMD. Decorate that line and the enhancer stops recognising it, so
    the hardening block is silently never injected.

    No apt layer: the full Debian node image already ships git and
    ca-certificates (proven by the enhancer's own HTTPS clone succeeding), and
    this repo needs nothing else from the OS.
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

        return f"""FROM {image_name}

{self.global_env}

{ENCODING_ENV}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """Per-PR layer: the patches, the stage scripts, and the warmed node_modules."""

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
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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
# `git status --porcelain` is empty only when there is nothing modified, staged
# or untracked - deliberately stricter than `git diff --quiet`, because the
# failure this catches is usually a leftover UNTRACKED file (`git clean -qfd`
# does not remove ignored files without -x).
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
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
# that exact commit by sha over the full URL. A fetch drags fresh git objects
# into an image whose history the base deliberately stripped, so the block
# below re-runs the scrub in exactly that case. On this instance the base was
# built from this very sha, so the fetch never runs.
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

# Install the dependency tree into the image layer so the three scored stages
# do not re-download it. The root install triggers the repo's own postinstall
# hook (scripts/install.sh), which runs `npm i` inside every packages/* - that
# is how this monorepo wires itself up in CI (no lerna bootstrap, no
# workspaces), so it is reproduced verbatim here. Everything lands in
# gitignored node_modules directories, so the tree stays clean for the guard.
npm install

# Warm run: fills nyc's cache and require caches; the outcome is irrelevant to
# grading, hence `|| true`.
{test_cmd} > /dev/null 2>&1 || true
git reset --hard
git clean -qfd
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
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
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
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
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
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
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
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


@Instance.register("istanbuljs", "istanbuljs")
class Istanbuljs(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # mocha's TAP reporter prints exactly one line per test:
        #   ok 12 TransformerTest maps branch locations
        #   not ok 13 TransformerTest maps switch statements
        #   ok 14 something # SKIP -
        # The name is mocha's fullTitle() (describe chain + test title), so ids
        # are stable across stages and unique across packages. Nothing else in
        # the log matches ^(not )?ok <number> - nyc's coverage table, npm
        # output and stack traces all fail the anchor - so no noise filter is
        # needed beyond the anchored regex itself.
        tap_line = re.compile(r"^(not )?ok (\d+) (.+?)\s*$")

        for raw in log.splitlines():
            m = tap_line.match(raw.strip())
            if not m:
                continue
            failed, name = bool(m.group(1)), m.group(3).strip()
            if re.search(r"#\s*SKIP", name, re.I):
                name = re.sub(r"\s*#\s*SKIP.*$", "", name, flags=re.I).strip()
                if name not in passed_tests and name not in failed_tests:
                    skipped_tests.add(name)
                continue
            if failed:
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
