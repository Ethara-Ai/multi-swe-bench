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

_ERA = "msw_2206_to_2000"

_PLAYWRIGHT_PIN = "1.44.1"

_ENV = """ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"""

_BUILD = """if ! corepack pnpm run build; then
    echo "Error: build failed" >&2
    exit 1
fi"""

_PREFLIGHT = """if ! npx --no-install vitest --version >/dev/null 2>&1; then
    echo "Error: vitest is not installed" >&2
    exit 1
fi
if ! npx --no-install playwright --version >/dev/null 2>&1; then
    echo "Error: playwright is not installed" >&2
    exit 1
fi
if [ -z "$(ls -A /ms-playwright 2>/dev/null)" ]; then
    echo "Error: no playwright browser installed under /ms-playwright" >&2
    exit 1
fi"""

_TEST_COMMANDS = """NODE_CONFIG=$(ls test/node/vitest.config.* 2>/dev/null | head -n 1 || true)
npx vitest run --reporter=tap-flat || true
if [ -n "$NODE_CONFIG" ]; then
    npx vitest run --reporter=tap-flat --config="$NODE_CONFIG" || true
fi
npx playwright test -c ./test/browser/playwright.config.ts --reporter=list || true"""


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
        return "node:18-bookworm"

    def image_tag(self) -> str:
        return "base-node18-pnpm"

    def workdir(self) -> str:
        return "base-node18-pnpm"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return base_dockerfile(self, ["rsync", "unzip"], _ENV)


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
        adds = [f'corepack pnpm add -D "@playwright/test@{_PLAYWRIGHT_PIN}"']
        if specs:
            adds.append("corepack pnpm add " + " ".join(f'"{spec}"' for spec in specs))
        adds.append("git checkout -- package.json pnpm-lock.yaml")
        extra_install = chr(10).join(adds)
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
corepack enable
corepack pnpm install --frozen-lockfile
{extra_install}
env -u PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD npx playwright install --with-deps chromium
{_BUILD}
{_PREFLIGHT}
npx vitest run --reporter=tap-flat || true
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
class MSW_VITEST(Instance):
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
    "2206",
    "2000",
]

for _ni in _BUNDLE_NIS:
    Instance.register(ORG, _ni)(MSW_VITEST)
