import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .storybook import (
    StorybookVersionBase,
    storybook_parse_log,
    _CHECK_GIT_CHANGES_SH,
    _CODE_EPOCH_PREPARE_SH,
    _CODE_EPOCH_RUN_TESTS_SH,
    _RUN_SH,
    _TEST_RUN_SH,
    _FIX_RUN_SH,
)

_NODE_IMAGE = "node:22"
_INTERVAL_NAME = "storybook_33864_to_13808"


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

    def dependency(self) -> Image:
        return StorybookVersionBase(
            self.pr, self._config, _NODE_IMAGE, _INTERVAL_NAME, use_corepack=True
        )

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def _test_files(self) -> list[str]:
        files = []
        for m in re.findall(r"diff --git a/(\S+)", self.pr.test_patch):
            if ".test." in m or ".spec." in m or ".test-d." in m or ".stories." in m:
                files.append(m)
        return files

    def files(self) -> list[File]:
        test_files = self._test_files()
        test_files_str = " ".join(test_files)

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                _CODE_EPOCH_PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                _CODE_EPOCH_RUN_TESTS_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.format(repo=self.pr.repo, test_files=test_files_str),
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


@Instance.register("storybookjs", _INTERVAL_NAME)
class StorybookCode(Instance):

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
        return storybook_parse_log(test_log)
