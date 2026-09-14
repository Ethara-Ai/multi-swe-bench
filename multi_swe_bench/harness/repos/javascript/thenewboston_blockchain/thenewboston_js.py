import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_BASE = "node:16-bullseye"

_TARGETS = (
    "TARGETS=$(grep -E '^\\+\\+\\+ b/' /home/test.patch 2>/dev/null "
    "| sed 's#^+++ b/##' | grep -E '\\.(test|spec)\\.[jt]sx?$' | sort -u | tr '\\n' ' ')\n"
    '[ -z "$TARGETS" ] && TARGETS=""\n'
    'echo "SCOPED TARGETS: $TARGETS"\n'
)

_RESET = "git reset --hard\ngit clean -fdq\n"

_SETUP = (
    "npm install --no-audit --no-fund --loglevel=error || npm install --no-audit --no-fund --legacy-peer-deps || true\n"
    "npm run build >/dev/null 2>&1 || npx rollup -c >/dev/null 2>&1 || true\n"
)

_TEST = "npx jest $TARGETS --ci --colors=false --verbose 2>&1 || true\n"


def _apply_one(patch: str) -> str:
    return (
        f"git apply --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --3way --whitespace=nowarn {patch} 2>/dev/null "
        f"|| git apply --whitespace=nowarn --include='*.js' --include='*.ts' {patch} 2>/dev/null || true\n"
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
        return _NODE_BASE

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
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 CI=true

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


@Instance.register("thenewboston-blockchain", "thenewboston-js")
class ThenewbostonJs(Instance):
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
        pass_re = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        fail_re = re.compile(r"^\s*[×✗✕]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
        skip_re = re.compile(r"^\s*[○✎]\s+(.+)$")
        for line in test_log.splitlines():
            m = pass_re.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
            m = fail_re.match(line)
            if m:
                failed_tests.add(m.group(1)); passed_tests.discard(m.group(1))
            m = skip_re.match(line)
            if m:
                skipped_tests.add(m.group(1))
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
