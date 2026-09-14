from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "node:14-bullseye"

APT_PACKAGES = "git ca-certificates python3 python-is-python3 ruby"

BASE_TAG = "base-9192_to_11576"

TEST_COMMAND = (
    './node_modules/.bin/mocha "test/unit/**/*.test.js" --reporter tap'
    ' --ignore "test/unit/scripts/serverless.test.js"'
)

RUN_ENV = (
    "export CI=true\n"
    "export FORCE_COLOR=1\n"
    'export SLS_DEPRECATION_DISABLE="*"\n'
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TAP_RE = re.compile(r"(not ok|ok) (\d+) ")
_SKIP_RE = re.compile(r"\s*#\s*(?:SKIP|skip|pending)\b.*$")

CHECK_GIT_CHANGES = """#!/bin/bash
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
"""


class ServerlessImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return BASE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = DockerfileEnhancer._infrastructure_block(self, BASE_IMAGE, True)
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {BASE_IMAGE}

{infra}
WORKDIR /home/

RUN apt-get -o Acquire::Check-Valid-Until=false update && \\
    apt-get install -y --no-install-recommends {APT_PACKAGES} \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class ServerlessImageDefault(Image):
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
        return ServerlessImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}\n"),
            File(".", "test.patch", f"{self.pr.test_patch}\n"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._stage_sh(patches=[])),
            File(".", "test-run.sh", self._stage_sh(patches=["test.patch"])),
            File(
                ".",
                "fix-run.sh",
                self._stage_sh(patches=["test.patch", "fix.patch"]),
            ),
        ]

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

export CI=true
export npm_config_audit=false
export npm_config_fund=false

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh

git checkout {sha}
bash /home/check_git_changes.sh

COMMIT_DATE="$(git show -s --format=%cI HEAD)"
echo "prepare: resolving dependencies as of ${{COMMIT_DATE}}"

npm install --before="${{COMMIT_DATE}}" --no-audit --no-fund --loglevel=error || true
test -x ./node_modules/.bin/mocha || npm install --no-audit --no-fund --loglevel=error || true

test -x ./node_modules/.bin/mocha
./node_modules/.bin/mocha --version
test -d test/unit
test "$(ls -1 node_modules | wc -l)" -gt 400
node -e "require('./package.json')"
node -e "require.resolve('mocha'); require.resolve('chai'); require.resolve('sinon'); require.resolve('@serverless/test/setup/log'); console.log('DEPS_OK')"

bash /home/check_git_changes.sh
"""

    def _stage_sh(self, patches: list[str]) -> str:
        repo = self.pr.repo
        applies = ""
        if patches:
            targets = " ".join(f"/home/{p}" for p in patches)
            applies = f"git apply --3way --whitespace=nowarn {targets}\n"
        return f"""#!/bin/bash
set -eo pipefail

{RUN_ENV}
cd /home/{repo}
{applies}
{TEST_COMMAND}
"""

    def dockerfile(self) -> str:
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)
        base = self.dependency().image_full_name()
        return f"""FROM {base}

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}"""


@Instance.register("serverless", "serverless")
class Serverless(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ServerlessImageDefault(self.pr, self._config)

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
        log = _ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in log.split("\n"):
            markers = list(_TAP_RE.finditer(line))
            if not markers:
                continue
            for i, m in enumerate(markers):
                end = markers[i + 1].start() if i + 1 < len(markers) else len(line)
                title = line[m.end() : end].strip()
                if not title:
                    continue
                if _SKIP_RE.search(title):
                    skipped_tests.add(_SKIP_RE.sub("", title).strip())
                elif m.group(1) == "ok":
                    passed_tests.add(title)
                else:
                    failed_tests.add(title)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
