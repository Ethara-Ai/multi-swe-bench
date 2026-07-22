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

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # The `# syntax` directive makes DockerfileEnhancer.enhance() return this
        # verbatim: no proxy/cert/MITM ARGs or ENV injected, and no rewrite of the
        # clone/COPY line above into a single-BASE_COMMIT pin + history strip (this
        # "base" tag is SHARED across every PR, so it must keep full history/no pin).
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN npm install -g pnpm
RUN apt update && apt install -y jq

{code}

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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves our dockerfile() verbatim,
        # so the hardening below is applied by hand (anchored on HEAD, since
        # BASE_COMMIT is not a build-arg in FROM-an-image builds).
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Defense-in-depth against re-fetching the fix from GitHub by URL. The
    # hardening block deletes the cloned repo's `origin`, but a model could still
    # run `git fetch https://github.com/<org>/<repo> <future_sha>` to pull the
    # commits that come AFTER the base (where the fix lives). We blackhole every
    # github URL scheme at the git --system level so any git transport to github
    # is rewritten to an unroutable address and fails fast. (Authoritative block
    # is still eval-time network isolation, `docker run --network none`; this is
    # the belt-and-suspenders that survives even a networked run.)
    _GIT_NET_LOCKDOWN = (
        'RUN BH="https://0.0.0.0:1/"; \\\n'
        '    git config --system url."$BH".insteadOf "https://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "http://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "ssh://git@github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git@github.com:"; \\\n'
        '    git config --system url."$BH".insteadOf "https://codeload.github.com/"; \\\n'
        '    git config --system protocol.allow never; \\\n'
        '    git config --system protocol.file.allow always; \\\n'
        '    git config --system --unset-all credential.helper 2>/dev/null || true'
    )

    def _harden(self) -> str:
        """Git-history hardening for the per-PR image, applied AFTER prepare.sh
        has checked out THIS PR's base commit -> the commit to KEEP is the
        current HEAD. Mirrors the harness Image._HARDENING_BLOCK, anchored on
        HEAD instead of ${BASE_COMMIT}."""
        repo = self.pr.repo
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
jq 'del(.pnpm.patchedDependencies)' package.json > package.tmp.json && mv package.tmp.json package.json
pnpm install  --no-frozen-lockfile

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
pnpm build
pnpm test -- --verbose --testLocationInResults

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
pnpm build
pnpm test -- --verbose --testLocationInResults

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
pnpm build
pnpm test -- --verbose --testLocationInResults

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self._GIT_NET_LOCKDOWN}

{self._harden()}

{self.clear_env}

"""


class ImageDefault11199(Image):
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
        # Returns an Image (the shared base) -> DockerfileEnhancer.enhance()
        # early-returns (dep is not a str) and leaves our dockerfile() verbatim,
        # so the hardening below is applied by hand (anchored on HEAD, since
        # BASE_COMMIT is not a build-arg in FROM-an-image builds).
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Defense-in-depth against re-fetching the fix from GitHub by URL. The
    # hardening block deletes the cloned repo's `origin`, but a model could still
    # run `git fetch https://github.com/<org>/<repo> <future_sha>` to pull the
    # commits that come AFTER the base (where the fix lives). We blackhole every
    # github URL scheme at the git --system level so any git transport to github
    # is rewritten to an unroutable address and fails fast. (Authoritative block
    # is still eval-time network isolation, `docker run --network none`; this is
    # the belt-and-suspenders that survives even a networked run.)
    _GIT_NET_LOCKDOWN = (
        'RUN BH="https://0.0.0.0:1/"; \\\n'
        '    git config --system url."$BH".insteadOf "https://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "http://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git://github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "ssh://git@github.com/"; \\\n'
        '    git config --system url."$BH".insteadOf "git@github.com:"; \\\n'
        '    git config --system url."$BH".insteadOf "https://codeload.github.com/"; \\\n'
        '    git config --system protocol.allow never; \\\n'
        '    git config --system protocol.file.allow always; \\\n'
        '    git config --system --unset-all credential.helper 2>/dev/null || true'
    )

    def _harden(self) -> str:
        """Git-history hardening for the per-PR image, applied AFTER prepare.sh
        has checked out THIS PR's base commit -> the commit to KEEP is the
        current HEAD. Mirrors the harness Image._HARDENING_BLOCK, anchored on
        HEAD instead of ${BASE_COMMIT}."""
        repo = self.pr.repo
        return f"""RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach HEAD; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
rm -rf node_modules
yarn --frozen-lockfile

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
yarn build
yarn test -- --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch
yarn build
yarn test -- --verbose

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply /home/test.patch /home/fix.patch
yarn build
yarn test -- --verbose

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self._GIT_NET_LOCKDOWN}

{self._harden()}

{self.clear_env}

"""


@Instance.register("remix-run", "react-router")
class ReactRouter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 40180:
            return ImageDefault11199(self.pr, self._config)
        # elif self.pr.number <= 33415:
        #     return MaterialUiImageDefault33415(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_suite = None

        re_pass_suite = re.compile(r"^PASS (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        re_fail_suite = re.compile(r"^FAIL (.+?)(?:\s\(\d*\.?\d+\s*\w+\))?$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass_suite.match(line)
            if pass_match:
                current_suite = pass_match.group(1)
                passed_tests.add(current_suite)

            fail_match = re_fail_suite.match(line)
            if fail_match:
                current_suite = fail_match.group(1)
                failed_tests.add(current_suite)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
