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
        return "node:18-bullseye"

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

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV CI=true
ENV NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/
RUN apt-get update && apt-get install -y git jq

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
        return ImageBase(self.pr, self.config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
npm ci || true
npm run build || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
npm run build || true
cd /home/{pr.repo}/packages/__tests__
npx mocha --ui bdd --reporter tap --colors=false --timeout 5000 --exclude "dist/esm/__tests__/integration/**/*.spec.js" --exclude "dist/esm/__tests__/store-v1/**/*.spec.js" dist/esm/__tests__/setup-node.js "dist/esm/__tests__/**/*.spec.js"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch
npm run build || true
cd /home/{pr.repo}/packages/__tests__
npx mocha --ui bdd --reporter tap --colors=false --timeout 5000 --exclude "dist/esm/__tests__/integration/**/*.spec.js" --exclude "dist/esm/__tests__/store-v1/**/*.spec.js" dist/esm/__tests__/setup-node.js "dist/esm/__tests__/**/*.spec.js"

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch
npm run build || true
cd /home/{pr.repo}/packages/__tests__
npx mocha --ui bdd --reporter tap --colors=false --timeout 5000 --exclude "dist/esm/__tests__/integration/**/*.spec.js" --exclude "dist/esm/__tests__/store-v1/**/*.spec.js" dist/esm/__tests__/setup-node.js "dist/esm/__tests__/**/*.spec.js"

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

{self.clear_env}

"""


@Instance.register("aurelia", "aurelia")
class Aurelia(Instance):
    _NPM_ENSURE = "[ -d node_modules ] || npm ci || true"
    _BUILD = "npm run build || true"
    _TEST_CMD = (
        'cd /home/aurelia/packages/__tests__ && '
        'npx mocha --ui bdd --reporter tap --colors=false --timeout 5000 '
        '--exclude "dist/esm/__tests__/integration/**/*.spec.js" '
        '--exclude "dist/esm/__tests__/store-v1/**/*.spec.js" '
        'dist/esm/__tests__/setup-node.js '
        '"dist/esm/__tests__/**/*.spec.js"'
    )

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
        return (
            f"bash -c 'cd /home/{self.pr.repo} && "
            f"{self._NPM_ENSURE} && {self._BUILD} && {self._TEST_CMD}'"
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            f"bash -c 'cd /home/{self.pr.repo} && "
            f"git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch && "
            f"{self._NPM_ENSURE} && {self._BUILD} && {self._TEST_CMD}'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            f"bash -c 'cd /home/{self.pr.repo} && "
            f"git apply --exclude package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch && "
            f"{self._NPM_ENSURE} && {self._BUILD} && {self._TEST_CMD}'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        re_ok = re.compile(r"^ok \d+\s+(.+)$")
        re_not_ok = re.compile(r"^not ok \d+\s+(.+)$")
        skip_re = re.compile(r"\s+#\s*SKIP\b.*$")

        for raw_line in test_log.splitlines():
            clean = ansi_re.sub("", raw_line).rstrip()
            if not clean:
                continue

            m_fail = re_not_ok.match(clean)
            if m_fail:
                failed_tests.add(m_fail.group(1).strip())
                continue

            m_ok = re_ok.match(clean)
            if m_ok:
                desc = m_ok.group(1)
                skip_match = skip_re.search(desc)
                if skip_match:
                    skipped_tests.add(desc[: skip_match.start()].strip())
                else:
                    passed_tests.add(desc.strip())

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
