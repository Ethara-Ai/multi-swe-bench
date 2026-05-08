from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .joplin import JoplinImageBase, joplin_parse_log, _CHECK_GIT_CHANGES_SH

_NODE_IMAGE = "node:18"
_INTERVAL_NAME = "joplin_5815_to_14847"


class ImageDefaultEra3(Image):

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
        return JoplinImageBase(
            self.pr, self._config, _NODE_IMAGE, _INTERVAL_NAME
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

export YARN_ENABLE_IMMUTABLE_INSTALLS=false
corepack enable
yarn install --mode=skip-build || yarn install || true
yarn run tsc 2>&1 || true

""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
npm run test-ci 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn --reject /home/test.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install --mode=skip-build || true
yarn run tsc 2>&1 || true
npm run test-ci 2>&1 || true

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export CI=true

cd /home/{repo}
git apply --exclude yarn.lock --whitespace=nowarn --reject /home/test.patch /home/fix.patch || true
rm -f *.rej **/*.rej 2>/dev/null || true
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
yarn install --mode=skip-build || true
yarn run tsc 2>&1 || true
npm run test-ci 2>&1 || true

""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        return """FROM {name}:{tag}

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

{clear_env}

""".format(
            name=name,
            tag=tag,
            global_env=self.global_env,
            copy_commands=copy_commands,
            clear_env=self.clear_env,
        )


@Instance.register("laurent22", _INTERVAL_NAME)
class JoplinEra3(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefaultEra3(self.pr, self._config)

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
        return joplin_parse_log(test_log)
