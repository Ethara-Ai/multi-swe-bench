import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class MemfreeImageBase(Image):
    """Repo-level base: node + `frontend/` npm install (Next.js monorepo, jest + ts-jest).

    memfree is a monorepo; the test-under-change is `frontend/lib/store/tests/
    local-history.test.ts` and the package (jest, ts-jest, typescript 5) lives in `frontend/`."""

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
        return "node:18-bullseye"

    def image_prefix(self) -> str:
        return "envagent"

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

        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y git python3 build-essential && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
# the jest/ts-jest toolchain lives in frontend/. --legacy-peer-deps: the Next app has a large
# peer graph; ignore-scripts avoids slow/optional native postinstalls we don't need for jest.
RUN cd frontend && (npm install --force --legacy-peer-deps --ignore-scripts \\
    || npm install --force --legacy-peer-deps || true)

{self.clear_env}

CMD ["/bin/bash"]
"""


class MemfreeImageDefault(Image):
    """PR-specific image: FROM the repo base, add only patches + run scripts."""

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
        return MemfreeImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # jest (via frontend/jest.config.js, ts-jest) scoped to the added test file.
        test_cmd = (
            "cd /home/{repo}/frontend && "
            "npx jest --verbose --ci lib/store/tests/local-history.test.ts 2>&1 || true"
        ).format(repo=self.pr.repo)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e
cd /home/{pr.repo}
git checkout {pr.base.sha}
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
{test_cmd}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        return f"""\
FROM {dep.image_name()}:{dep.image_tag()}

{self.global_env}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY prepare.sh /home/prepare.sh
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("memfreeme", "memfree")
class MEMFREE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MemfreeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # jest --verbose prints `✓/✕/○ name (Xms)` per test, indented under the describe.
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        for raw in clean.split("\n"):
            line = raw.rstrip()
            m = re.match(r"^\s*[✓√]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line)
            if m:
                passed.add(m.group(1).strip())
                continue
            m = re.match(r"^\s*[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", line)
            if m:
                failed.add(m.group(1).strip())
                continue
            m = re.match(r"^\s*[○–-]\s+(.+?)\s+\(skipped\)\s*$", line)
            if m:
                skipped.add(m.group(1).strip())
        passed -= failed
        passed -= skipped
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
