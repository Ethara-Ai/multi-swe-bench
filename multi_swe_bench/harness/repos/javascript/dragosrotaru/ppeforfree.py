import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# dragosrotaru/ppeforfree#58 "Update home page" is a Create-React-App project (react-scripts
# 3.4.1, React 16, @testing-library/react 9). PR touches src/App.tsx + src/pages/about/*; the
# tests (src/__tests__/App.test.tsx, src/__tests__/about/index.tsx) render <App/>/<About/> in
# jsdom and assert the new home-page copy. `react-scripts test` runs jest once under CI=true.
# node:14 — react-scripts 3.4.1 predates the node-17 OpenSSL break.


class PpeforfreeImageBase(Image):
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
        return "node:14-bullseye"

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
ENV CI=true
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN npm install --force || true

{self.clear_env}

"""


class PpeforfreeImageDefault(Image):
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
        return PpeforfreeImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # CI=true makes react-scripts test run once (no watch). --verbose so jest prints one
        # ✓/✕ line per test for parse_log. --testPathPattern limits to the graded suites.
        test_cmd = (
            "CI=true npx react-scripts test --watchAll=false --verbose "
            "--testPathPattern='src/__tests__' 2>&1"
        )
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
git reset --hard
git checkout {pr.base.sha}
rm -rf node_modules
npm install --force || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{cmd}
""".format(pr=self.pr, cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
npm install --force || true
{cmd}
""".format(pr=self.pr, cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
npm install --force || true
{cmd}
""".format(pr=self.pr, cmd=test_cmd),
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
WORKDIR /home/{self.pr.repo}
RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("dragosrotaru", "ppeforfree")
class Ppeforfree(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PpeforfreeImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        # jest --verbose prints per-test lines: "  ✓ name (12 ms)" / "  ✕ name" / "  ○ skipped name"
        line_re = re.compile(r"^\s*([✓✕✗√×○✎])\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        for line in clean.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            sym, name = m.group(1), m.group(2).strip()
            if sym in ("✓", "√"):
                passed.add(name)
            elif sym in ("✕", "✗", "×"):
                failed.add(name)
            else:  # ○ skipped / ✎ todo
                skipped.add(name)
        passed -= failed
        skipped -= failed
        skipped -= passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
