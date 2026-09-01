import re
from typing import Union

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
        # The full `python:3.11` (Debian bookworm), NOT `-slim`. The reference
        # base structure goes straight from `WORKDIR /home/` to
        # `RUN git clone`, with no apt layer — which is only correct when the
        # runtime image already carries git and a compiler. `node:14` and
        # `rust:1.91` do; `python:3.11-slim` does not (it ships neither git nor
        # gcc), which is why the slim variant would force an apt block that the
        # reference structure has no slot for. The full image ships git, gcc,
        # and pkg-config, so no package layer is needed at all.
        return "python:3.11"

    def image_tag(self) -> str:
        # P1 requires the PR layer to inherit `…:base-pr-<N>`, so the base must
        # publish under that tag — not a bare `base`.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Emit the reference base structure verbatim. The shared generator
        # (Image.dockerfile) hardcodes an apt layer and re-declares
        # DEBIAN_FRONTEND/LANG after `WORKDIR /home/`; neither appears in the
        # reference, and neither is suppressible through `extra_packages()`.
        # Overriding here keeps the deviation inside this repo's config rather
        # than editing shared harness code.
        #
        # Everything below the FROM line — the ARG/ENV/LABEL/CA-farm infra —
        # is still injected by DockerfileEnhancer at pipeline level, exactly as
        # for every other repo in the registry.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo

        # The two extra blank lines before WORKDIR match the reference exactly
        # (the enhancer's infra block ends with one newline; the reference has
        # four blank lines total between the CA farm and WORKDIR).
        return f"""FROM {image_name}



WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]
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

    def dependency(self) -> Image:
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
git clean -fdq
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1
python -V

# NOT `|| true`: an install that fails quietly would ship an image with a
# broken environment and leave every graded act collecting zero tests, which
# reads as "resolved" for all the wrong reasons.
python -m pip install --no-cache-dir --upgrade pip setuptools wheel
python -m pip install --no-cache-dir -e ".[dev]"
python -m pytest tests/tui_gateway/test_protocol.py --collect-only -q -p no:cacheprovider -n 0

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
python -m pytest tests/tui_gateway/test_protocol.py \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
python -m pytest tests/tui_gateway/test_protocol.py \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
python -m pytest tests/tui_gateway/test_protocol.py \\
    -p no:cacheprovider -n 0 \\
    -v --no-header -rA --tb=no --continue-on-collection-errors 2>&1

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


@Instance.register("NousResearch", "hermes-agent")
class NousResearchHermesAgent(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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

        cleaned = re.sub(r"\x1b\[[0-9;]*m", "", test_log)

        # path::Class::test STATUS [ N%]
        re_standard = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)"
            r"(?:\s+\[.*\])?\s*$"
        )

        # [gwN] [ N%] STATUS path::Class::test
        re_xdist = re.compile(
            r"^\[gw\d+\]\s+\[\s*\d+%\]\s+"
            r"(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+(\S+::\S+)"
        )

        # `-rA` short-summary form: STATUS path::Class::test [reason].
        # SKIPPED/XFAIL carry a bracketed count first: `SKIPPED [1] path::test`.
        re_summary = re.compile(
            r"^(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\s+"
            r"(?:\[\d+\]\s+)?(\S+::\S+)"
        )

        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_xdist.match(line) or re_summary.match(line)
            if m:
                status = m.group(1)
                test_name = m.group(2)
            else:
                m = re_standard.match(line)
                if m:
                    test_name = m.group(1)
                    status = m.group(2)
                else:
                    continue

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

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
