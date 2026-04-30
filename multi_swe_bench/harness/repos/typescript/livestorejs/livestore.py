import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Packages whose postinstall scripts must be allowed for vitest/esbuild to work
PNPM_ALLOWED_BUILD_DEPS = [
    "esbuild",
    "protobufjs",
    "sharp",
    "workerd",
    "@parcel/watcher",
    "msgpackr-extract",
    "dtrace-provider",
    "puppeteer",
    "cbor-extract",
    "@mixedbread/cli",
    "@tailwindcss/oxide",
]

# jq filter to inject pnpm.onlyBuiltDependencies into package.json
JQ_INJECT_BUILD_DEPS = (
    '.pnpm.onlyBuiltDependencies = ['
    + ", ".join(f'"{dep}"' for dep in PNPM_ALLOWED_BUILD_DEPS)
    + "]"
)


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
RUN apt update && apt install -y git jq
RUN npm install -g pnpm@10

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
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Inject pnpm.onlyBuiltDependencies to allow esbuild postinstall
jq '{jq_filter}' package.json > package.json.tmp && mv package.json.tmp package.json

# Downgrade vitest 4.x -> 3.2.4 in catalog if present (patches assume vitest 3.x config)
if [ -f pnpm-workspace.yaml ]; then
  sed -i 's/vitest: 4\\.[0-9]\\+\\.[0-9]\\+/vitest: 3.2.4/' pnpm-workspace.yaml
fi

export WORKSPACE_ROOT=/home/{pr.repo}
export NODE_OPTIONS="--disable-warning=ExperimentalWarning"
export CI=1

pnpm install || true

# Ensure vitest is available at root (PR #1089+ has it only in per-package devDeps)
pnpm add -Dw vitest || true

""".format(pr=self.pr, jq_filter=JQ_INJECT_BUILD_DEPS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export WORKSPACE_ROOT=/home/{pr.repo}
export NODE_OPTIONS="--disable-warning=ExperimentalWarning"
export CI=1

npx vitest run --reporter=verbose 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export WORKSPACE_ROOT=/home/{pr.repo}
export NODE_OPTIONS="--disable-warning=ExperimentalWarning"
export CI=1

git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch || git apply --exclude pnpm-lock.yaml --whitespace=nowarn --3way /home/test.patch || true
pnpm install || true
npx vitest run --reporter=verbose 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export WORKSPACE_ROOT=/home/{pr.repo}
export NODE_OPTIONS="--disable-warning=ExperimentalWarning"
export CI=1

git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --exclude pnpm-lock.yaml --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true
pnpm install || true
npx vitest run --reporter=verbose 2>&1 || true

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


@Instance.register("livestorejs", "livestore")
class Livestore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def _test_cmd(self) -> str:
        return (
            "export WORKSPACE_ROOT=/home/livestore"
            " && export NODE_OPTIONS='--disable-warning=ExperimentalWarning'"
            " && export CI=1"
            " && npx vitest run --reporter=verbose 2>&1 || true"
        )

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd

        return f"bash -c 'cd /home/{self.pr.repo}; {self._test_cmd()}'"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo};"
            f" git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch"
            f" || git apply --exclude pnpm-lock.yaml --whitespace=nowarn --3way /home/test.patch"
            f" || true;"
            f" pnpm install || true;"
            f" {self._test_cmd()}'"
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd

        return (
            f"bash -c 'cd /home/{self.pr.repo};"
            f" git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch"
            f" || git apply --exclude pnpm-lock.yaml --whitespace=nowarn --3way /home/test.patch /home/fix.patch"
            f" || true;"
            f" pnpm install || true;"
            f" {self._test_cmd()}'"
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # Vitest verbose file-level results:
        #   Pass: ✓ |@livestore/common| src/sync/syncstate.test.ts > ... 5ms
        #   Fail: × |@livestore/utils| src/node/.../file.test.ts > ... 10003ms
        # We extract: @livestore/<pkg> <file.test.ts> as the test identifier
        re_pass = re.compile(
            r"[✓✔]\s+(?:\|?@livestore/[\w-]+\|?)\s+(\S+\.(?:test|spec)\.tsx?)"
        )
        re_fail = re.compile(
            r"[❯×✗]\s+(?:\|?@livestore/[\w-]+\|?)\s+(\S+\.(?:test|spec)\.tsx?)"
        )

        for line in test_log.splitlines():
            clean = ansi_re.sub("", line).strip()
            if not clean:
                continue

            pass_match = re_pass.search(clean)
            if pass_match:
                passed_tests.add(pass_match.group(1))
                continue

            fail_match = re_fail.search(clean)
            if fail_match:
                failed_tests.add(fail_match.group(1))

        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
