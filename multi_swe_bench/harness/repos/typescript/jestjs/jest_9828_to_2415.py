from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class _ImageBase(Image):
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
        return "node:10-buster"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo_name}\n"
                "git reset --hard\n"
                f"git checkout {sha}\n"
                ""
                "yarn install --network-timeout 600000 || true\n"
                "yarn run postinstall || true\n"
                "yarn build || node ./scripts/build.js || true\n",
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo_name}\n"
                ""
                "yarn install --network-timeout 600000 || true\n"
                "yarn run postinstall || true\n"
                "yarn build || node ./scripts/build.js || true\n"
                "yarn jest --verbose 2>&1 || true\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo_name}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch; then\n"
                '    echo \"Error: git apply failed\" >&2\n'
                "    exit 1\n"
                "fi\n"
                ""
                "yarn install --network-timeout 600000 || true\n"
                "yarn run postinstall || true\n"
                "yarn build || node ./scripts/build.js || true\n"
                "yarn jest --verbose 2>&1 || true\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                "set -e\n"
                f"cd /home/{repo_name}\n"
                "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
                '    echo \"Error: git apply failed\" >&2\n'
                "    exit 1\n"
                "fi\n"
                ""
                "yarn install --network-timeout 600000 || true\n"
                "yarn run postinstall || true\n"
                "yarn build || node ./scripts/build.js || true\n"
                "yarn jest --verbose 2>&1 || true\n",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """FROM node:10-buster

ENV DEBIAN_FRONTEND=noninteractive

RUN sed -i "s/deb.debian.org/archive.debian.org/g" /etc/apt/sources.list && sed -i "s/security.debian.org/archive.debian.org/g" /etc/apt/sources.list && sed -i "/buster-updates/d" /etc/apt/sources.list && apt-get update && apt-get install -y git

WORKDIR /home/
COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{org}/{repo}.git /home/{repo}

WORKDIR /home/{repo}
RUN git reset --hard
RUN git checkout {sha}

{copy_commands}

RUN yarn install --frozen-lockfile --network-timeout 600000 --ignore-scripts || yarn install --network-timeout 600000 --ignore-scripts || yarn install --ignore-scripts || true

""".format(
            org=self.pr.org,
            repo=self.pr.repo,
            sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


@Instance.register("jestjs", "jest_9828_to_2415")
class Jest_9828_to_2415(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _ImageBase(self.pr, self._config)

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
        return _parse_jest_log(test_log)


def _parse_jest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    passed_res = [
        re.compile(r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
        re.compile(r"^\s*[✓✔]\s+(.+)$"),
    ]
    failed_res = [
        re.compile(r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
        re.compile(r"^\s*[×✗✕]\s+(.+)$"),
    ]
    skipped_res = [
        re.compile(r"^\s*SKIP\s+(.+?)(?:\s+\(\d+[\.\d]*\s*s\))?$"),
    ]

    for line in test_log.splitlines():
        for pat in passed_res:
            m = pat.match(line)
            if m and m.group(1) not in failed_tests:
                passed_tests.add(m.group(1))
        for pat in failed_res:
            m = pat.match(line)
            if m:
                failed_tests.add(m.group(1))
                passed_tests.discard(m.group(1))
        for pat in skipped_res:
            m = pat.match(line)
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
