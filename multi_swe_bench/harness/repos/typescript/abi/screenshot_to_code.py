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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl jq ca-certificates \\
        python3 python3-pip python3-venv python3-dev pipx \\
        build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN pipx ensurepath && pipx install poetry
ENV PATH="/root/.local/bin:${{PATH}}"
RUN pip3 install --break-system-packages --quiet pytest pytest-asyncio || true
RUN npm install -g yarn pnpm@9 || true

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
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Backend: install Python deps via poetry (best-effort)
if [ -f backend/pyproject.toml ]; then
  cd backend
  poetry env use "$(which python3)" || true
  poetry install --no-interaction --no-root || true
  cd ..
fi

# Frontend: install Node deps (best-effort; favor yarn since lockfiles are yarn)
if [ -f frontend/package.json ]; then
  cd frontend
  if [ -f yarn.lock ]; then
    yarn install --frozen-lockfile || yarn install || true
  elif [ -f package-lock.json ]; then
    npm ci || npm install || true
  else
    yarn install || npm install || true
  fi
  cd ..
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                ("""#!/bin/bash
set +e

REPO_DIR="/home/__REPO__"
cd "$REPO_DIR"

echo "===== BACKEND PYTEST ====="
if [ -d backend ] && find backend -name 'test_*.py' -print -quit 2>/dev/null | grep -q .; then
  cd backend
  if [ -f pyproject.toml ]; then
    poetry run pytest -v 2>&1 || pytest -v 2>&1 || true
  else
    pytest -v 2>&1 || true
  fi
  cd "$REPO_DIR"
fi

echo "===== FRONTEND TESTS ====="
if [ -d frontend ] && [ -f frontend/package.json ]; then
  cd frontend
  HAS_TEST=$(grep -E '"test"\\s*:' package.json | head -1)
  if [ -n "$HAS_TEST" ]; then
    CI=true yarn test --verbose 2>&1 || CI=true yarn test 2>&1 || true
  fi
  cd "$REPO_DIR"
fi

exit 0

""").replace("__REPO__", self.pr.repo),
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


@Instance.register("abi", "screenshot-to-code")
class ScreenshotToCode(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # pytest verbose: "tests/test_foo.py::TestClass::test_name PASSED [ 12%]"
        re_pytest = re.compile(
            r"^(\S+::\S+|\S+\.py(?:::\S+)?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        # jest default reporter: "PASS src/foo/bar.test.ts" / "FAIL src/foo/bar.test.ts"
        re_jest_file = re.compile(r"^(PASS|FAIL)\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b")

        # jest verbose / vitest per-test: "✓ test name (12 ms)" or "✕ test name"
        re_jest_pass_test = re.compile(r"^\s*✓\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_jest_fail_test = re.compile(r"^\s*✕\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_jest_skip_test = re.compile(r"^\s*○\s+skipped\s+(.+?)\s*$")

        # vitest: "✓ file.test.ts (N tests)" / "× file.test.ts"
        re_vitest_pass = re.compile(r"^\s*✓\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b")
        re_vitest_fail = re.compile(r"^\s*[×✗]\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b")

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line)
            stripped = line.strip()
            if not stripped:
                continue

            m = re_pytest.match(stripped)
            if m:
                name, status = m.group(1), m.group(2)
                if status == "PASSED" or status == "XPASS":
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(name)
                continue

            m = re_jest_file.match(stripped)
            if m:
                status, name = m.group(1), m.group(2)
                if status == "PASS":
                    passed_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            m = re_vitest_pass.match(stripped)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_vitest_fail.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_jest_skip_test.match(stripped)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_jest_pass_test.match(stripped)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_jest_fail_test.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
