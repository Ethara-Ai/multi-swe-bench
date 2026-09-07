import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_ENTRYPOINT = "test/test.js"

_SUBMODULE_STRIP = (
    "git checkout --detach HEAD; "
    "git remote remove origin 2>/dev/null || true; "
    'git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace '
    "| xargs -r -n1 git update-ref -d; "
    "git reflog expire --expire=now --all; "
    "git reflog expire --expire-unreachable=now --all; "
    "git gc --prune=now --aggressive; "
    "rm -f .git/objects/info/alternates;"
)


def _harden_git(repo_dir: str, sha: str, number: int) -> str:
    return f"""RUN set -eux; \\
    cd {repo_dir}; \\
    git cat-file -e {sha}^{{commit}} 2>/dev/null \\
        || git fetch --no-tags origin +refs/pull/{number}/head:refs/remotes/origin/pr-{number}; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN set -eux; \\
    cd {repo_dir}; \\
    if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive '{_SUBMODULE_STRIP}'; \\
    fi
"""


class ROLLUP_2240_TO_2240_ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:8-buster"

    def image_tag(self) -> str:
        return "base-2240_to_2240"

    def workdir(self) -> str:
        return "base-2240_to_2240"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git \\\n"
                f"    clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY ./{self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class ROLLUP_2240_TO_2240_ImageDefault(Image):
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
        return ROLLUP_2240_TO_2240_ImageBase(self.pr, self._config)

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

npm install --ignore-scripts || npm install || true
npm run build:cjs || npm run build || true

git checkout -- .
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
npm run test:only -- --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --exclude=package-lock.json --whitespace=nowarn /home/test.patch
npm run build:cjs || npm run build
npm run test:only -- --reporter json

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --exclude=package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build:cjs || npm run build
npm run test:only -- --reporter json

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

        harden = _harden_git(f"/home/{self.pr.repo}", self.pr.base.sha, self.pr.number)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
{harden}
RUN bash /home/prepare.sh

{self.clear_env}

"""


class ROLLUP_2240_TO_2240(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ROLLUP_2240_TO_2240_ImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        test_log = re.sub(
            r"^(=+|Writing .*|npm ERR!.*|npm WARN.*|npm notice.*)$",
            "",
            test_log,
            flags=re.MULTILINE,
        )
        test_log = test_log.replace("\r\n", "").replace("\n", "")

        repo_prefix = f"/home/{self.pr.repo}/"

        def test_name(test: dict) -> str:
            title = test.get("fullTitle") or test.get("title") or ""
            path = test.get("file") or _TEST_ENTRYPOINT
            if path.startswith(repo_prefix):
                path = path[len(repo_prefix) :]
            return f"{path}::{title}" if path else title

        decoder = json.JSONDecoder()
        pos = 0
        while True:
            match = test_log.find("{", pos)
            if match == -1:
                break
            try:
                result, index = decoder.raw_decode(test_log[match:])
                if isinstance(result, dict) and "stats" in result:
                    for test in result.get("passes", []):
                        name = test_name(test)
                        if name:
                            passed_tests.add(name)
                    for test in result.get("failures", []):
                        name = test_name(test)
                        if name:
                            failed_tests.add(name)
                    for test in result.get("pending", []):
                        name = test_name(test)
                        if name:
                            skipped_tests.add(name)
                pos = match + index
            except ValueError:
                pos = match + 1

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
