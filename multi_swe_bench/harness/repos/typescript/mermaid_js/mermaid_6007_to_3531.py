from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

from .mermaid import (
    MermaidVersionBase,
    mermaid_vitest_parse_log,
    _CHECK_GIT_CHANGES_SH,
    _PNPM_PREPARE_SH,
    _PNPM_RUN_SH,
    _PNPM_TEST_RUN_SH,
    _PNPM_FIX_RUN_SH,
)

_NODE_IMAGE = "node:18-bookworm"
_INTERVAL_NAME = "mermaid_6007_to_3531"


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
        return MermaidVersionBase(
            self.pr,
            self._config,
            _NODE_IMAGE,
            _INTERVAL_NAME,
            pkg_manager="pnpm_with_yarn_fallback",
            pnpm_version="9",
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
                _PNPM_PREPARE_SH.format(
                    repo=self.pr.repo, base_sha=self.pr.base.sha
                ),
            ),
            File(
                ".",
                "run.sh",
                _PNPM_RUN_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                _PNPM_TEST_RUN_SH.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                _PNPM_FIX_RUN_SH.format(repo=self.pr.repo),
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


@Instance.register("mermaid-js", _INTERVAL_NAME)
class MermaidPnpmVitest18(Instance):

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
        return mermaid_vitest_parse_log(test_log)
