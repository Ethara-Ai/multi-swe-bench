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
        return "node:22"

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
        return ImageBase(self.pr, self._config)

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
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Detect tap major version and unit script from package.json
TAP_MAJOR=$(node -e "try{{let t=require('./package.json').devDependencies.tap||'0';console.log(t.replace(/[^0-9.]/g,'').split('.')[0])}}catch(e){{console.log('0')}}")
UNIT_CMD=$(node -e "try{{console.log(require('./package.json').scripts.unit||'')}}catch(e){{console.log('')}}")

# tap@12-15 loads esm by default which crashes on Node 18+.
# PRs that pass --no-esm to tap are safe on Node 22.
if [ "$TAP_MAJOR" -ge 12 ] 2>/dev/null && [ "$TAP_MAJOR" -le 15 ] 2>/dev/null; then
    if echo "$UNIT_CMD" | grep -qv -- '--no-esm'; then
        echo "prepare.sh: tap@$TAP_MAJOR without --no-esm detected, installing Node 16"
        curl -fsSL https://deb.nodesource.com/setup_16.x | bash -
        apt-get install -y --allow-downgrades nodejs
        # Override Node 22 in /usr/local/bin with newly installed Node 16
        ln -sf /usr/bin/node /usr/local/bin/node
        ln -sf /usr/bin/npm /usr/local/bin/npm
        hash -r
        echo "prepare.sh: Node version now $(node --version)"
    fi
fi

npm install --legacy-peer-deps || npm install --legacy-peer-deps --ignore-scripts || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
if node -e "process.exit(require('./package.json').scripts.unit ? 0 : 1)" 2>/dev/null; then
    npm run unit -- --reporter=spec
else
    npx tap test/*.test.js test/*/*.test.js --reporter=spec
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package.json --whitespace=nowarn /home/test.patch
if node -e "process.exit(require('./package.json').scripts.unit ? 0 : 1)" 2>/dev/null; then
    npm run unit -- --reporter=spec
else
    npx tap test/*.test.js test/*/*.test.js --reporter=spec
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply  --exclude package.json --whitespace=nowarn /home/test.patch /home/fix.patch
if node -e "process.exit(require('./package.json').scripts.unit ? 0 : 1)" 2>/dev/null; then
    npm run unit -- --reporter=spec
else
    npx tap test/*.test.js test/*/*.test.js --reporter=spec
fi


""".format(pr=self.pr),
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


@Instance.register("fastify", "fastify")
class Fastify(Instance):
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
        if "borp" in test_log[:500]:
            return self._parse_borp(test_log)
        # TAP v14 emits top-level "ok N - ..." lines; mocha-spec does not.
        if re.search(r"^ok \d+ - ", test_log, re.MULTILINE):
            return self._parse_tap(test_log)
        return self._parse_mocha_spec(test_log)

    def _parse_tap(self, test_log: str) -> TestResult:
        """Parse TAP v14 output from `tap --reporter=spec`."""
        re_skip = re.compile(r"^ok \d+ - (.+?)\s+# (?:SKIP|skip|TODO|todo).*$", re.MULTILINE)
        re_pass = re.compile(r"^ok \d+ - (.+?)(?:\s+#.*)?$", re.MULTILINE)
        re_fail = re.compile(r"^not ok \d+ - (.+?)(?:\s+#.*)?$", re.MULTILINE)

        skipped_tests = set(re_skip.findall(test_log))
        passed_tests = set(re_pass.findall(test_log)) - skipped_tests
        failed_tests = set(re_fail.findall(test_log))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_borp(self, test_log: str) -> TestResult:
        """Parse borp --reporter=spec output (Node test runner)."""
        re_pass = re.compile(r"^\s*✔ (.+?)\s+\([\d.]+m?s\)\s*$", re.MULTILINE)
        re_fail = re.compile(r"^\s*✖ (.+?)\s+\([\d.]+m?s\)\s*$", re.MULTILINE)
        re_skip = re.compile(r"^\s*﹣ (.+?)(?:\s+\([\d.]+m?s\))?\s*(?:# SKIP)?\s*$", re.MULTILINE)

        passed_tests = set(re_pass.findall(test_log))
        failed_tests = set(re_fail.findall(test_log))
        skipped_tests = set(re_skip.findall(test_log))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_mocha_spec(self, test_log: str) -> TestResult:
        """Parse mocha-spec output from older tap versions.

        Format: file names at column 0 (``test/foo.test.js``), indented
        checkmarks for passes, numbered failures referencing file names
        (``  N) test/foo.test.js ...``).  Granularity is per-file — the
        same level used by TAP v14 and borp parsers.
        """
        re_all_files = re.compile(
            r"^(test/\S+\.(?:test\.js|test\.mjs|test\.cjs))$", re.MULTILINE
        )
        re_fail_files = re.compile(
            r"^\s+\d+\)\s+(test/\S+\.(?:test\.js|test\.mjs|test\.cjs))\s",
            re.MULTILINE,
        )

        all_files = set(re_all_files.findall(test_log))
        failed_tests = set(re_fail_files.findall(test_log))
        passed_tests = all_files - failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=0,
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=set(),
        )
