"""mermaid-js/mermaid-live-editor harness config (yarn 1 + vitest 0.22.1, jsdom)."""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ENV_PREAMBLE = """\
export CI=true
export NODE_NO_WARNINGS=1
"""

_TEST_CMD = "npx vitest run --reporter=verbose"


class MermaidLiveEditorImageBase(Image):
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        default_packages = [
            "ca-certificates",
            "build-essential",
            "curl",
            "git",
            "gnupg",
            "make",
            "python3",
            "wget",
        ]
        packages_str = " \\\n    ".join(default_packages)
        apt_command = self._get_apt_update_command(packages_str, image_name)

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/

{apt_command}

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}
"""


class MermaidLiveEditorImageDefault(Image):
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
        return MermaidLiveEditorImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        sections = [f"FROM {base.image_full_name()}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(f"WORKDIR /home/{self.pr.repo}")
        sections.append(copy_commands.rstrip("\n"))
        sections.append("RUN bash /home/prepare.sh")
        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"

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
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export CYPRESS_INSTALL_BINARY=0

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

git checkout {base_sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {base_sha})"
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --non-interactive

test -d node_modules/uuid
test -d .svelte-kit
test -x node_modules/.bin/vitest
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

{env}
cd /home/{repo}

{test_cmd}
""".format(repo=self.pr.repo, env=_ENV_PREAMBLE, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{env}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{test_cmd}
""".format(repo=self.pr.repo, env=_ENV_PREAMBLE, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

{env}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_cmd}
""".format(repo=self.pr.repo, env=_ENV_PREAMBLE, test_cmd=_TEST_CMD),
            ),
        ]


@Instance.register("mermaid-js", "mermaid-live-editor")
class MermaidLiveEditor(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MermaidLiveEditorImageDefault(self.pr, self._config)

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
        """Parse `vitest run --reporter=verbose` output (vitest 0.22.1, flat layout)."""
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        test_re = re.compile(r"^ {0,2}([✓×↓])\s+(\S.*?)\s*$")

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).rstrip()
            if not line.strip():
                continue

            match = test_re.match(line)
            if not match:
                continue

            marker, name = match.group(1), match.group(2).strip()
            if not name:
                continue

            if marker == "✓":
                passed_tests.add(name)
            elif marker == "×":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
