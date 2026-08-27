import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ENV_SETUP = (
    "export CI=true\n"
    'export NODE_OPTIONS="--max-old-space-size=4096"\n'
    'export PATH="/home/tsoa/node_modules/.bin:/home/tsoa/tests/node_modules/.bin:$PATH"'
)

TEST_CMD = 'NODE_ENV=tsoa_test mocha "**/*.spec.ts" --reporter tap'

DEDUPE_ENV_RE = re.compile(
    r"^ENV (?:DEBIAN_FRONTEND=noninteractive|LANG=C\.UTF-8)\n", re.MULTILINE
)


class TsoaImageBase(Image):
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return DEDUPE_ENV_RE.sub("", super().dockerfile())


class TsoaImageDefault(Image):
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
        return TsoaImageBase(self.pr, self._config)

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
                "build.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{repo}

yarn --cwd /home/{repo}/packages/runtime run build
yarn --cwd /home/{repo}/packages/cli run build
""".format(repo=self.pr.repo, env=ENV_SETUP),
            ),
            File(
                ".",
                "pretest.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{repo}/tests

yarn run clean || true
yarn run prepare-test || true
yarn run typecheck || true
""".format(repo=self.pr.repo, env=ENV_SETUP),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

{env}

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

yarn install --ignore-scripts --frozen-lockfile || true

bash /home/check_git_changes.sh

test -x node_modules/.bin/mocha

bash /home/build.sh || true
bash /home/pretest.sh || true
""".format(repo=self.pr.repo, env=ENV_SETUP),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{repo}

bash /home/build.sh
bash /home/pretest.sh

cd /home/{repo}/tests

{test_cmd} 2>&1
""".format(repo=self.pr.repo, env=ENV_SETUP, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

if grep -qE '^diff --git a/(package\\.json|yarn\\.lock|packages/[^/]+/package\\.json|tests/package\\.json)' /home/test.patch; then
    yarn install --ignore-scripts || true
fi

bash /home/build.sh
bash /home/pretest.sh

cd /home/{repo}/tests

{test_cmd} 2>&1
""".format(repo=self.pr.repo, env=ENV_SETUP, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

if grep -qhE '^diff --git a/(package\\.json|yarn\\.lock|packages/[^/]+/package\\.json|tests/package\\.json)' /home/test.patch /home/fix.patch; then
    yarn install --ignore-scripts || true
fi

bash /home/build.sh
bash /home/pretest.sh

cd /home/{repo}/tests

{test_cmd} 2>&1
""".format(repo=self.pr.repo, env=ENV_SETUP, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_command = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("lukeautry", "tsoa")
class LukeautryTsoa(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return TsoaImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_ok = re.compile(r"^ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = re_not_ok.match(line)
            if m:
                name = re_directive.sub("", m.group(1)).strip()
                if name:
                    failed_tests.add(name)
                continue

            m = re_ok.match(line)
            if m:
                raw_name = m.group(1)
                is_skip = bool(re_directive.search(raw_name))
                name = re_directive.sub("", raw_name).strip()
                if not name:
                    continue
                if is_skip:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

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
