from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.javascript.handsontable.handsontable import (
    APPLY_FIX,
    APPLY_TEST,
    CHECK_GIT_CHANGES,
    CHECKOUT,
    HARDENING,
    ImageBase,
    ORG,
    PUPPETEER_PREFLIGHT,
    PUPPETEER_PREFLIGHT_BUILD,
    REPO,
    SHEBANG,
    RUNNER_PATCH,
    RUN_SUITE,
    parse_handsontable_log,
    pr_dockerfile,
)

_ERA = "handsontable_10655_to_8906"

_WORKSPACE = f"cd /home/{REPO}/{REPO}"

_TEST_COMMANDS = f"""{RUN_SUITE}

run_suite unit npm run test:unit -- --verbose

npm run build:walkontable
npm run test:walkontable.dump
run_suite walkontable npm run test:walkontable.puppeteer

npm run build:umd
npm run build:languages
npm run test:e2e.dump
run_suite e2e npm run test:e2e.puppeteer"""


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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd /home/{REPO}
export PHANTOMJS_PLATFORM=linux
export PHANTOMJS_ARCH=x64
{CHECKOUT}
bash /home/check_git_changes.sh
{HARDENING}
npm ci --legacy-peer-deps
{_WORKSPACE}
{RUNNER_PATCH}
{PUPPETEER_PREFLIGHT_BUILD}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{_WORKSPACE}
{PUPPETEER_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register(ORG, _ERA)
class HANDSONTABLE_10655_TO_8906(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        return parse_handsontable_log(log)


_BUNDLE_NIS = [
    "10655",
    "9117",
    "9102",
    "8906",
]

for _ni in _BUNDLE_NIS:
    Instance.register(ORG, _ni)(HANDSONTABLE_10655_TO_8906)
