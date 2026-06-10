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
RUN apt-get update && apt-get install -y git jq curl
RUN npm install -g --force pnpm@9.15.4

{code}

# embedded-postgres (used by server/db tests) refuses to run as root, so all
# install/build/test steps execute as the non-root "node" user shipped in the
# official node image. Hand the cloned repo and the user's HOME to that user.
RUN chown -R node:node /home/{self.pr.repo} /home/node

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
git config --global --add safe.directory /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
chown -R node:node /home/{pr.repo} /home/node
su node -c 'cd /home/{pr.repo} && export CI=true HOME=/home/node && pnpm install --frozen-lockfile' \\
  || su node -c 'cd /home/{pr.repo} && export CI=true HOME=/home/node && pnpm install' \\
  || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

# Best effort: re-enable IPv6 loopback so servers bound to :: are reachable
# via ::1. Harmless (and silently ignored) when the daemon forbids it.
sysctl -w net.ipv6.conf.lo.disable_ipv6=0 >/dev/null 2>&1 || true

cd /home/{pr.repo}
su node -c 'cd /home/{pr.repo} && export CI=true HOME=/home/node PATH=/usr/local/bin:$PATH NODE_OPTIONS="--max-old-space-size=4096 --dns-result-order=ipv4first" && pnpm exec vitest run --reporter=verbose'

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

sysctl -w net.ipv6.conf.lo.disable_ipv6=0 >/dev/null 2>&1 || true

cd /home/{pr.repo}
su node -c 'cd /home/{pr.repo} && git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch && export CI=true HOME=/home/node PATH=/usr/local/bin:$PATH NODE_OPTIONS="--max-old-space-size=4096 --dns-result-order=ipv4first" && pnpm exec vitest run --reporter=verbose'

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

sysctl -w net.ipv6.conf.lo.disable_ipv6=0 >/dev/null 2>&1 || true

cd /home/{pr.repo}
su node -c 'cd /home/{pr.repo} && git apply --exclude pnpm-lock.yaml --whitespace=nowarn /home/test.patch /home/fix.patch && export CI=true HOME=/home/node PATH=/usr/local/bin:$PATH NODE_OPTIONS="--max-old-space-size=4096 --dns-result-order=ipv4first" && pnpm exec vitest run --reporter=verbose'

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


@Instance.register("paperclipai", "paperclip")
class Paperclip(Instance):
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

        # Strip ANSI escape codes for reliable matching.
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # vitest 3 verbose reporter, one line per test case:
        #   " ✓  paperclipai  src/__tests__/foo.test.ts > suite > name 12ms"
        #   " ×  @paperclipai/server  src/__tests__/bar.test.ts > suite > name 1ms"
        #   " ↓  @paperclipai/ui  src/baz.test.tsx > suite > name"
        # Group 1 = status symbol, group 2 = "<project> <file> > <suite...> > <name>".
        line_re = re.compile(
            r"^\s*([✓×✗↓])\s+"
            r"(\S.*?\.(?:test|spec)\.tsx?\s*>\s*.+?)"
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
        )

        for raw in test_log.splitlines():
            line = ansi_re.sub("", raw).rstrip()
            if not line:
                continue

            match = line_re.match(line)
            if not match:
                continue

            symbol = match.group(1)
            name = re.sub(r"\s+", " ", match.group(2)).strip()

            if symbol == "✓":  # ✓ passed
                passed_tests.add(name)
            elif symbol == "↓":  # ↓ skipped / todo
                skipped_tests.add(name)
            else:  # × or ✗ failed
                failed_tests.add(name)

        # A retried test can surface as both pass and fail; treat any failure
        # observation as authoritative.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
