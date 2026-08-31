import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class VibetunnelImageBase(Image):
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
        return "node:22-bookworm"

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
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y git libpam0g-dev \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@10

{code}

{self.clear_env}

"""


class VibetunnelImageDefault(Image):
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
        return VibetunnelImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
                """\
#!/bin/bash
set -e

export PUPPETEER_SKIP_DOWNLOAD=true

step() {{
    label="$1"; shift
    echo "===== prepare: ${{label}} ====="
    if ! "$@"; then
        echo "prepare: FAILED at '${{label}}' -- aborting the image build." >&2
        exit 1
    fi
}}

fetch_base() {{
    git cat-file -e {base_sha} 2>/dev/null \\
        || git fetch --quiet https://github.com/{org}/{repo}.git {base_sha}
}}

install_web() {{
    pnpm install --frozen-lockfile || pnpm install
}}

build_node_pty() {{
    cd node-pty && npm install && npm run build && cd ..
}}

cd /home/{repo}

step "reset worktree" git reset --hard
step "verify clean tree after reset" bash /home/check_git_changes.sh
step "fetch base commit {base_sha}" fetch_base
step "checkout {base_sha}" git checkout {base_sha}
step "verify clean tree after checkout" bash /home/check_git_changes.sh
step "locate web directory" test -d web

cd web

step "pnpm install" install_web
step "build node-pty" build_node_pty
""".format(org=self.pr.org, repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}/web
CI=true npx vitest run --reporter=verbose --no-file-parallelism
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

cd web
CI=true npx vitest run --reporter=verbose --no-file-parallelism
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

cd web
CI=true npx vitest run --reporter=verbose --no-file-parallelism
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("VibetunnelImageDefault dependency must be an Image")
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


@Instance.register("amantus-ai", "vibetunnel")
class VIBETUNNEL(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return VibetunnelImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]|\x00", "", test_log)

        spec_re = r"\S+\.(?:test|spec)\.[cm]?[jt]sx?"
        duration_re = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?"
        case_re = re.compile(
            rf"^\s*(?P<marker>[✓✔√×✕✖✗✘↓○])\s+"
            rf"(?P<name>{spec_re}\s+>\s+.*?)"
            rf"{duration_re}\s*$",
            re.MULTILINE,
        )
        fail_case_re = re.compile(
            rf"^\s*FAIL\s+(?P<name>{spec_re}\s+>\s+.+?)\s*$", re.MULTILINE
        )

        pass_markers = {"✓", "✔", "√"}
        fail_markers = {"×", "✕", "✖", "✗", "✘"}
        skip_markers = {"↓", "○"}

        for m in case_re.finditer(clean_log):
            marker = m.group("marker")
            name = m.group("name").strip()
            if marker in pass_markers:
                passed_tests.add(name)
            elif marker in fail_markers:
                failed_tests.add(name)
            elif marker in skip_markers:
                skipped_tests.add(name)

        for m in fail_case_re.finditer(clean_log):
            failed_tests.add(m.group("name").strip())

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
