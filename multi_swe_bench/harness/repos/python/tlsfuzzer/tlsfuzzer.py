import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PY_BASE = "python:3.9-slim-bookworm"

_TARGETS = (
    "TARGETS=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null "
    "| sed 's#^+++ b/##' | grep -E '^tests/.*\\.py$' | sort -u | tr '\\n' ' ')\n"
    '[ -z "$TARGETS" ] && TARGETS="tests/"\n'
    'echo "SCOPED TARGETS: $TARGETS"\n'
)

_RESET = "git reset --hard\ngit clean -fdq\n"

_SETUP = (
    "python -m pip install --no-cache-dir --upgrade pip setuptools wheel >/dev/null 2>&1 || true\n"
    "python -m pip install --no-cache-dir 'tlslite-ng>=0.8.2' 'ecdsa>=0.15' >/dev/null 2>&1 || true\n"
    "python -m pip install --no-cache-dir pytest 'mock>2.0.0' >/dev/null 2>&1 || true\n"
    "python -m pip install --no-cache-dir -e . >/dev/null 2>&1 || true\n"
)

_TEST = (
    "python -m pytest $TARGETS -p no:cacheprovider -v -rA --no-header --tb=no "
    "--continue-on-collection-errors 2>&1\n"
)


def _apply_one(patch: str) -> str:
    return (
        f"git apply --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --3way --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --whitespace=nowarn --include='*.py' {patch} 2>/dev/null "
        f"|| git apply --3way --whitespace=nowarn --include='*.py' {patch} 2>/dev/null "
        f"|| true\n"
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
        return _PY_BASE

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
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get -o Acquire::Check-Valid-Until=false -o Acquire::Retries=5 update && apt-get install -y --no-install-recommends git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo}
WORKDIR /home/{repo}

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
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo}\n"
                f"git cat-file -e {self.pr.base.sha}^{{commit}} 2>/dev/null \\\n"
                f"    || git fetch --no-tags --depth=2147483647 origin {self.pr.base.sha} \\\n"
                f'    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"\n'
                "git reset --hard\n"
                "git clean -fdq\n"
                f"git checkout --detach {self.pr.base.sha}\n"
                "git clean -fdq\n"
                + _SETUP,
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET + _SETUP + _TARGETS + _TEST,
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _SETUP + _TARGETS + _TEST,
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\nset -e\n" f"cd /home/{repo}\n" + _RESET
                + _apply_one("/home/test.patch") + _apply_one("/home/fix.patch") + _SETUP + _TARGETS + _TEST,
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("tlsfuzzer", "tlsfuzzer")
class TlsFuzzer(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        ansi = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
        log = ansi.sub("", test_log)
        node = r"[^\s\[]+::[^\s\[]+(?:\[[^\]]*\])?"
        statuses = r"PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS"
        prog = re.compile(rf"^(?P<node>{node})\s+(?P<status>{statuses})")
        summ = re.compile(rf"^(?P<status>{statuses})\s+(?P<node>{node})(?:\s+-\s+.*)?$")
        buckets = {
            "PASSED": passed_tests, "XPASS": passed_tests,
            "FAILED": failed_tests, "ERROR": failed_tests,
            "SKIPPED": skipped_tests, "XFAIL": skipped_tests,
        }
        for line in log.splitlines():
            line = line.strip()
            m = prog.match(line) or summ.match(line)
            if m:
                buckets[m.group("status")].add(m.group("node"))
        skipped_tests -= failed_tests
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
