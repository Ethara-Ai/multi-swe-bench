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
        return "node:lts-bookworm"

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

        sections = [
            f"FROM {image_name}",
            self.global_env,
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends git jq xvfb && rm -rf /var/lib/apt/lists/*",
            code,
            self.clear_env,
        ]
        return "\n".join(s for s in sections if s) + "\n"


_PREPARE_SCRIPT = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

npm install --no-audit --no-fund || true
if [ -d webview-ui ]; then
  (cd webview-ui && npm install --no-audit --no-fund) || true
fi
if [ -d cli ] && [ -f cli/package.json ]; then
  (cd cli && npm install --no-audit --no-fund) || true
fi
"""


_GIT_APPLY_EXCLUDES = (
    "--exclude=package-lock.json "
    "--exclude=webview-ui/package-lock.json "
    "--exclude=cli/package-lock.json "
)


_TEST_SCRIPT = """set +e
cd /home/{repo}

if npm run 2>/dev/null | grep -qE '^  protos$'; then
  npm run protos > /dev/null 2>&1 || true
fi

REQ_ARGS="--require ts-node/register --require source-map-support/register"
if [ -f src/test/requires.ts ]; then
  REQ_ARGS="$REQ_ARGS --require ./src/test/requires.ts"
fi

if [ -f .mocharc.json ] || [ -f .mocharc.js ] || [ -f .mocharc.cjs ]; then
  TS_NODE_PROJECT=./tsconfig.unit-test.json npx --no-install mocha --reporter spec $REQ_ARGS 2>&1
fi

if npm run 2>/dev/null | grep -qE '^  compile-tests$'; then
  npm run compile-tests 2>&1 || true
  if compgen -G 'out/**/*.test.js' > /dev/null; then
    npx --no-install mocha --reporter spec --timeout 20000 'out/**/*.test.js' 2>&1 || true
  fi
fi

if [ -d webview-ui ] && [ -f webview-ui/package.json ]; then
  (cd webview-ui && npx --no-install vitest run --reporter=verbose 2>&1) || true
fi

if [ -d cli ] && [ -f cli/package.json ]; then
  (cd cli && npx --no-install vitest run --reporter=verbose 2>&1) || true
fi
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SCRIPT.format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n" + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
                + "git apply --reject --exclude=package-lock.json --exclude=webview-ui/package-lock.json --exclude=cli/package-lock.json --whitespace=nowarn /home/test.patch || true\n"
                + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\ncd /home/{repo}\n".format(repo=self.pr.repo)
                + "git apply --reject --exclude=package-lock.json --exclude=webview-ui/package-lock.json --exclude=cli/package-lock.json --whitespace=nowarn /home/test.patch /home/fix.patch || true\n"
                + _TEST_SCRIPT.format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        sections = [
            f"FROM {name}:{tag}",
            self.global_env,
            copy_commands,
            "RUN bash /home/prepare.sh",
            self.clear_env,
        ]
        return "\n".join(s for s in sections if s) + "\n"


@Instance.register("cline", "cline")
class Cline(Instance):
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

        ansi_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

        mocha_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\w+\))?\s*$")
        mocha_fail_num = re.compile(r"^\s*\d+\)\s+(.+?)\s*$")
        mocha_pending = re.compile(r"^\s*-\s+(.+?)\s*$")

        vitest_pass = re.compile(r"^\s*[✓✔]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\(\d+ms\))?\s*$")
        vitest_fail = re.compile(r"^\s*[×✗✘]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\(\d+ms\))?\s*$")
        vitest_skip = re.compile(r"^\s*[↓⊝]\s+(?:\S+\s+)?(\S+\.(?:test|spec)\.[jt]sx?)(?:\s+>\s+(.+?))?(?:\s+\(\d+ms\))?\s*$")

        in_failure_listing = False

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).rstrip()
            if not line:
                continue

            if re.match(r"^\s*\d+\s+failing", line):
                in_failure_listing = True
                continue
            if re.match(r"^\s*\d+\s+passing", line):
                in_failure_listing = False
                continue
            if re.match(r"^\s*\d+\s+pending", line):
                continue

            m = vitest_pass.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                passed_tests.add(name)
                continue

            m = vitest_fail.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                failed_tests.add(name)
                continue

            m = vitest_skip.match(line)
            if m:
                name = f"{m.group(1)} > {m.group(2)}" if m.group(2) else m.group(1)
                skipped_tests.add(name)
                continue

            if in_failure_listing:
                m = mocha_fail_num.match(line)
                if m:
                    failed_tests.add(m.group(1))
                    continue

            m = mocha_pass.match(line)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = mocha_pending.match(line)
            if m:
                name = m.group(1)
                if not name.startswith("--") and len(name) < 200:
                    skipped_tests.add(name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
