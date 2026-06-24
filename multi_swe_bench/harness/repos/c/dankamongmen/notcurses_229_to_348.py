from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class NotcursesImageBaseMid(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return "ubuntu:20.04"

    def image_tag(self) -> str:
        return "base-229_to_348"

    def workdir(self) -> str:
        return "base-229_to_348"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TERM=xterm \\
    COLORTERM=truecolor \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    cmake \\
    git \\
    pkg-config \\
    ca-certificates \\
    libncurses-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libavcodec-dev \\
    libswscale-dev \\
    libtinfo-dev \\
    libreadline-dev \\
    doctest-dev \\
    python3-dev \\
    python3-setuptools \\
    python3-cffi \\
    util-linux \\
    zlib1g-dev \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class NotcursesImageDefaultMid(Image):
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
        return NotcursesImageBaseMid(self.pr, self._config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
else
  echo "notcurses-tester binary not built; build likely failed"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard {pr.base.sha}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
else
  echo "notcurses-tester binary not built; build likely failed"
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash

cd /home/{pr.repo}
git reset --hard {pr.base.sha}
if [ -s /home/test.patch ]; then
  git apply --whitespace=nowarn --reject /home/test.patch 2>/dev/null || true
fi
if [ -s /home/fix.patch ]; then
  git apply --whitespace=nowarn --reject /home/fix.patch 2>/dev/null || true
fi
rm -rf build
mkdir -p build && cd build
cmake .. -DUSE_PANDOC=OFF -DCMAKE_BUILD_TYPE=Release 2>&1 || true
cmake --build . -j$(nproc) 2>&1 || true
if [ -x ./notcurses-tester ]; then
  unset TERM
  echo "===== NOTCURSES TESTER XML ====="
  ./notcurses-tester --reporters=xml 2>&1 || true
  echo "===== END XML ====="
else
  echo "notcurses-tester binary not built; build likely failed"
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
        # Per-PR FULL hardening (prepare.sh has checked out base.sha): strip refs +
        # gc-prune so future/fix commits are unreachable & deleted, then audit.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("dankamongmen", "notcurses_229_to_348")
class NOTCURSES_229_TO_348(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NotcursesImageDefaultMid(self.pr, self._config)

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

        clean = re.sub(r"\x1b\[[?0-9;]*[a-zA-Z]", "", test_log)

        re_xml_testcase = re.compile(
            r'<TestCase\s+name="([^"]+)"[^>]*>(.*?)</TestCase>',
            re.DOTALL,
        )
        re_xml_failures = re.compile(
            r'<OverallResultsAsserts[^>]*failures="(\d+)"'
        )
        for m in re_xml_testcase.finditer(clean):
            name = m.group(1)
            body = m.group(2)
            fa = re_xml_failures.search(body)
            if fa and int(fa.group(1)) > 0:
                failed_tests.add(name)
            else:
                passed_tests.add(name)

        if not passed_tests and not failed_tests:
            re_doctest_failure_name = re.compile(r"TEST CASE:\s+(\S.*?)\s*$", re.MULTILINE)
            failing_named = re_doctest_failure_name.findall(clean)
            re_doctest_summary = re.compile(
                r"\[doctest\]\s*test cases:\s*\d+\s*\|\s*(\d+)\s*passed\s*\|\s*(\d+)\s*failed\s*\|\s*(\d+)\s*skipped"
            )
            sm = re_doctest_summary.search(clean)
            if sm:
                n_pass, n_fail, n_skip = (int(sm.group(i)) for i in (1, 2, 3))
                for nm in failing_named[:n_fail]:
                    failed_tests.add(nm.strip())
                while len(failed_tests) < n_fail:
                    failed_tests.add(f"doctest_failed_{len(failed_tests)}")
                for i in range(n_pass):
                    passed_tests.add(f"doctest_passed_{i}")
                for i in range(n_skip):
                    skipped_tests.add(f"doctest_skipped_{i}")

        re_ctest_pass = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s+Passed\s+.*$"
        )
        re_ctest_fail_variants = [
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Failed\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Exception.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Not Run\s+.*$"),
            re.compile(r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\*\*\*Timeout\s+.*$"),
        ]
        re_ctest_skip = re.compile(
            r"^\d+/\d+\s*Test\s*#\d+:\s*(.*?)\s*\.+\s*Skipped\s+.*$"
        )
        for line in clean.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re_ctest_pass.match(line)
            if m:
                passed_tests.add(m.group(1).strip())
            for r in re_ctest_fail_variants:
                m = r.match(line)
                if m:
                    failed_tests.add(m.group(1).strip())
            m = re_ctest_skip.match(line)
            if m:
                skipped_tests.add(m.group(1).strip())

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


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_NOTCURSES_229_TO_348 = [
    '344-345-348-350',
    '247-248-250-251',
    '261-264-266',
    '271-273-274-278-282-287',
    '300-301',
    '312-315-319',
    '327-331',
]
for _ni in _BUNDLE_NIS_NOTCURSES_229_TO_348:
    Instance.register('dankamongmen', _ni)(NOTCURSES_229_TO_348)
