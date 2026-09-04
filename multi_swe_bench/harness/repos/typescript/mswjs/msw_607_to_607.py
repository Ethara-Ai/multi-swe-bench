from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.mswjs.msw import (
    APPLY_FIX,
    APPLY_TEST,
    CHECK_GIT_CHANGES,
    ORG,
    REPO,
    SHEBANG,
    base_dockerfile,
    extra_dep_specs,
    parse_msw_log,
    pr_dockerfile,
)

_ERA = "msw_607_to_607"

_PACKAGES = ["chromium", "fonts-liberation"]

_ENV = """ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"
ENV CHROME_PATH=/usr/bin/chromium"""

_BUILD = """npx rimraf lib native/lib node/lib || true
if ! NODE_ENV=production npx rollup -c rollup.config.ts; then
    echo "Error: build failed" >&2
    exit 1
fi"""

_PREFLIGHT = """if [ ! -f node_modules/jest/bin/jest.js ]; then
    echo "Error: jest not found at node_modules/jest/bin/jest.js" >&2
    exit 1
fi"""

_TEST_COMMANDS = """BABEL_ENV=test npx jest --config=jest.config.js --runInBand --verbose --ci --bail=0 || true
node --max_old_space_size=8000 node_modules/jest/bin/jest.js --config=test/jest.config.js --runInBand --verbose --ci --bail=0 || true"""


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
        return "node:12-bullseye"

    def image_tag(self) -> str:
        return "base-node12-yarn"

    def workdir(self) -> str:
        return "base-node12-yarn"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self, _PACKAGES, _ENV)


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
        specs = extra_dep_specs(self.pr)
        extra_install = (
            "yarn add " + " ".join(f'"{spec}"' for spec in specs) + "\n"
            "git checkout -- package.json yarn.lock"
            if specs
            else "true"
        )
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd /home/{REPO}
bash /home/check_git_changes.sh
yarn install --frozen-lockfile --ignore-engines --network-timeout 600000
{extra_install}
{_BUILD}
{_PREFLIGHT}
BABEL_ENV=test npx jest --config=jest.config.js --runInBand --verbose --ci --bail=0 || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{_BUILD}
{_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{_BUILD}
{_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{_BUILD}
{_PREFLIGHT}
{_TEST_COMMANDS}
""",
            ),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self)


@Instance.register(ORG, _ERA)
class MSW_JEST_NODE12(Instance):
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
        return parse_msw_log(log)


_BUNDLE_NIS = [
    "607",
]

for _ni in _BUNDLE_NIS:
    Instance.register(ORG, _ni)(MSW_JEST_NODE12)
