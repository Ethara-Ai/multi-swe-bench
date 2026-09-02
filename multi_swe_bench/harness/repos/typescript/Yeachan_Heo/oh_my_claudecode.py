"""Repo config for Yeachan-Heo/oh-my-claudecode (TypeScript, npm, vitest).

Known limitation, measured rather than assumed: a handful of upstream tests in
``src/hooks/permission-handler/__tests__/index.test.ts`` embed raw newline and
carriage-return bytes inside their own test names. Vitest's reporter is line
oriented, so ``parse_log`` only ever sees the first fragment of such a name.
The measured cost is exactly one collision per stage -- the two parameterised
``shell metacharacter injection prevention`` cases collapse onto a single
parsed name (3676 raw pass markers -> 3675 distinct names in pr-527/run.log).
The truncation is deterministic and identical in all three stages, so
cross-stage matching stays consistent and no spurious transition can be
manufactured; the only effect is one merged p2p entry out of ~3700.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OhMyClaudecodeImageBase(Image):
    """Shared base image: toolchain, clone, checkout and history hardening."""

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
        return "node:20"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The checkout and hardening below use a literal sha instead of
        # ${BASE_COMMIT} because build_dataset collects images into a set keyed
        # on image_full_name(), so the five per-PR base objects collapse to the
        # one built from dataset row 0 (PR 527, depth 801) and the build would
        # be stamped with that row's base.sha. Pruning here to depth 801 would
        # delete the base commits of PRs 574/895, 637/986 and 640/994, and the
        # same block removes ``origin``, so they could never be refetched. All
        # five base SHAs are ancestors of b1d53ae7 (the newest in the dataset),
        # so pinning the newest keeps every one of them reachable, while each
        # PR image re-runs the identical hardening on its own base.sha at the
        # end of prepare.sh -- pruning everything newer, gold merge commits
        # included. Dropping the ``# syntax`` directive would not remove this
        # hardening; it would opt the file back into DockerfileEnhancer
        # (image.py:317), which splices its own infrastructure block after the
        # first FROM and would therefore emit a second copy of the block this
        # file already renders (measured: 88 lines with the directive, 127
        # without, with ARG TARGETARCH and the OCI title LABEL duplicated).
        base_commit = "b1d53ae7034e32c864c43c7f0296d71fc9ae1d14"
        infrastructure = DockerfileEnhancer._infrastructure_block(self, image_name)
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", base_commit)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infrastructure}
{self.global_env}


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {base_commit}

{hardening}
{self.clear_env}
CMD ["/bin/bash"]
"""


class OhMyClaudecodeImageDefault(Image):
    """PR-specific image: checkout, stage scripts, install, harden."""

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
        return OhMyClaudecodeImageBase(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git cat-file -e {self.pr.base.sha} 2>/dev/null || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout {self.pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

# Dependencies are installed at build time only (never at evaluation time) so
# the three run stages need no network. The package's ``prepare`` lifecycle
# script runs a full ``tsc`` build that is allowed to fail, hence ``|| true``.
npm install --no-audit --no-fund || true

# ``npm install`` and the ``prepare`` build may rewrite tracked files
# (package-lock.json, the committed dist/ tree) and drop new untracked build
# output. Both would make the test/fix patches fail to apply, so restore the
# worktree to a pristine {self.pr.base.sha} checkout. ``git clean -fd`` omits ``-x``
# and therefore keeps the ignored node_modules/ directory.
git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

# Anti-reward-hack hardening, relocated here from the base image so that every
# PR is pinned to its own base.sha: detach, drop the remote, delete every ref,
# expire the reflog, then prune, so no commit newer than {self.pr.base.sha} -- and in
# particular no gold fix commit -- survives in this image. The four asserts make
# the build fail closed if any step is skipped.
git config --global --add safe.directory /home/{self.pr.repo}
git checkout --detach {self.pr.base.sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD
        git remote remove origin 2>/dev/null || true
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d
        git reflog expire --expire=now --all
        git reflog expire --expire-unreachable=now --all
        git gc --prune=now --aggressive
        rm -f .git/objects/info/alternates
    '
fi

""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{self.pr.repo}

npx --no-install vitest run --reporter=verbose --no-file-parallelism 2>&1

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch

npx --no-install vitest run --reporter=verbose --no-file-parallelism 2>&1

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -e

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch

npx --no-install vitest run --reporter=verbose --no-file-parallelism 2>&1

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("The dependency of the default image must be an image.")

        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The clone comes from the shared base image; this layer only stages the
        # patches and scripts and then runs prepare.sh, which performs this PR's
        # own checkout, dependency install and history hardening.
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Yeachan-Heo", "oh-my-claudecode")
class OhMyClaudecode(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeImageDefault(self.pr, self._config)

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
        # Strip ANSI escape codes first
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # === Vitest test-level pass (✓/✔) ===
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\d+(?:\.\d+)?\s*m?s)?$", stripped)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            # === Vitest test-level fail (×/✕/✗) ===
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\d+(?:\.\d+)?\s*m?s)?$", stripped)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            # === Vitest file-level FAIL ===
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            # === Vitest skipped (↓/○) ===
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

        # If a test appears in both pass and fail, it failed
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
