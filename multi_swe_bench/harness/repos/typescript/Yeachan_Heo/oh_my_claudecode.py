import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# oh-my-claudecode: Yeachan-Heo/oh-my-claudecode
# ---------------------------------------------------------------------------
# TypeScript CLI/plugin project. npm (package-lock.json). node >=20.
#
# 10 PRs in dataset, range #258 - #764.
# 1 era: node >=20, vitest ^4.0.17, test="vitest", prepare="npm run build" --
#   verified unchanged at both 89d57ea2 (#258) and 58e85243 (#764).
#
# Base image clones only -- no checkout, no pin, no hardening (see the project
#   reference Dockerfile). Each PR image checks out its own base.sha and runs
#   the full hardening in prepare.sh, so the finished image is still pruned to
#   that commit with origin removed.
#
# PR 348: base orphaned upstream by a force-push; reachable from no ref, but
#   GitHub serves it by full sha -- prepare.sh refetches it.
# PR 742: fix patch carries a payload-less binary hunk -> _binary_excludes.
#
# Image chain: node:20 -> ImageBase (clone) -> ImageDefault (checkout + harden)
#
# Test output: vitest verbose
#   ✓ src/f.test.ts > suite > name 2ms   -- pass
#   × src/f.test.ts > suite > name 5ms   -- fail
#   ↓ src/f.test.ts > suite > name       -- skip
#   FAIL src/f.test.ts [ src/f.test.ts ] -- suite failed to collect


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

        infrastructure = DockerfileEnhancer._infrastructure_block(self, image_name)

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {image_name}

{infrastructure}
{self.global_env}


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

{self.clear_env}
CMD ["/bin/bash"]
"""


class OhMyClaudecodeImageDefault(Image):
    """PR-specific image: checkout, stage scripts, install, harden."""

    @staticmethod
    def _binary_excludes(patch: str) -> str:
        """``--exclude`` flags for binary hunks the patch cannot carry."""
        flags = []
        for block in re.split(r"(?m)^diff --git ", patch)[1:]:
            m = re.match(r"a/(\S+) b/(\S+)", block)
            if not m:
                continue
            if (
                re.search(r"(?m)^Binary files .* differ$", block)
                and "GIT binary patch" not in block
            ):
                flags.append(f"--exclude={shlex.quote(m.group(2))}")
        return "".join(f" {flag}" for flag in flags)

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
        test_excludes = self._binary_excludes(self.pr.test_patch)
        fix_excludes = self._binary_excludes(self.pr.fix_patch)

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
if ! git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null; then
    if ! git remote get-url origin >/dev/null 2>&1; then
        git remote add origin "https://github.com/{self.pr.org}/{self.pr.repo}.git"
    fi
    if ! git fetch --quiet --no-tags origin {self.pr.base.sha}; then
        echo "prepare.sh: base commit {self.pr.base.sha} is unreachable and"
        echo "  upstream will not serve it by sha; this row cannot be built."
        exit 1
    fi
fi
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh

npm install --no-audit --no-fund || true

git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

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
set -eo pipefail

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
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn{test_excludes} /home/test.patch

npx --no-install vitest run --reporter=verbose --no-file-parallelism 2>&1

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn{test_excludes} /home/test.patch
git apply --whitespace=nowarn{fix_excludes} /home/fix.patch

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\d+(?:\.\d+)?\s*m?s)?$", stripped)
            if m:
                passed_tests.add(m.group(1).strip())
                continue

            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\d+(?:\.\d+)?\s*m?s)?$", stripped)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                failed_tests.add(m.group(1).strip())
                continue

            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                skipped_tests.add(m.group(1).strip())
                continue

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
