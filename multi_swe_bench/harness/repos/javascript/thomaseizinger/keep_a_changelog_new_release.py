import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# thomaseizinger/keep-a-changelog-new-release#22 "Fix spacing in Markdown lists" is a TS GitHub
# Action tested with jest + ts-jest. The test.patch adds CHANGELOG.md -> CHANGELOG.expected.md
# fixture pairs; updateChangelog.test.ts processes each and compares. The fix bumps
# remark-stringify 7.x -> 8.x (package.json/yarn.lock) AND uses the 8.x-only
# `.data("settings", {listItemIndent, tightDefinitions, bullet})` API in src/updateChangelog.ts.
# => the fix stage MUST re-run `yarn install` to actually install remark-stringify 8.x, otherwise
# the old 7.x is used and the fix has no effect. node:14 (ts-jest 24 / typescript 3.7 era).
# Validated: test stage 6 failed / fix stage 20 passed -> the 6 updateChangelog fixtures are f2p.

_TEST_CMD = "npx jest 2>&1"


class KeepAChangelogImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self): return self._pr
    @property
    def config(self): return self._config

    def dependency(self): return "node:14-bullseye"
    def image_prefix(self): return "envagent"
    def image_tag(self): return "base"
    def workdir(self): return "base"
    def files(self): return []

    def dockerfile(self):
        image_name = self.dependency()
        if isinstance(image_name, Image): image_name = image_name.image_full_name()
        return f"""\
FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV CI=true
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN npm i -g yarn >/dev/null 2>&1 || true

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}
RUN git checkout {self.pr.base.sha}
RUN yarn install --frozen-lockfile --network-timeout 600000 || yarn install --network-timeout 600000 || true

{self.clear_env}

"""


class KeepAChangelogImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self): return self._pr
    @property
    def config(self): return self._config

    def dependency(self): return KeepAChangelogImageBase(self.pr, self._config)
    def image_prefix(self): return "envagent"
    def image_tag(self): return f"pr-{self.pr.number}"
    def workdir(self): return f"pr-{self.pr.number}"

    def files(self):
        reset = "git reset --hard HEAD >/dev/null 2>&1 || true; git clean -fdq -e node_modules >/dev/null 2>&1 || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard >/dev/null 2>&1 || true
git checkout {pr.base.sha}
""".format(pr=self.pr)),
            File(".", "run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{reset}
{cmd}
""".format(pr=self.pr, reset=reset, cmd=_TEST_CMD)),
            File(".", "test-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{reset}
git apply --whitespace=nowarn /home/test.patch || true
{cmd}
""".format(pr=self.pr, reset=reset, cmd=_TEST_CMD)),
            # the fix bumps remark-stringify -> 8.x, so re-install before running
            File(".", "fix-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{reset}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true
yarn install --network-timeout 600000 >/dev/null 2>&1 || true
{cmd}
""".format(pr=self.pr, reset=reset, cmd=_TEST_CMD)),
        ]

    def dockerfile(self):
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


@Instance.register("thomaseizinger", "keep-a-changelog-new-release")
class KeepAChangelog(Instance):
    def __init__(self, pr, config, *args, **kwargs):
        super().__init__(); self._pr = pr; self._config = config
    @property
    def pr(self): return self._pr
    def dependency(self): return KeepAChangelogImageDefault(self.pr, self._config)
    def run(self, c=""): return c or "bash /home/run.sh"
    def test_patch_run(self, c=""): return c or "bash /home/test-run.sh"
    def fix_patch_run(self, c=""): return c or "bash /home/fix-run.sh"

    def parse_log(self, test_log):
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set(); failed: set[str] = set(); skipped: set[str] = set()
        line_re = re.compile(r"^\s*([✓✕✗√×○])\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        for line in clean.splitlines():
            m = line_re.match(line)
            if not m: continue
            sym, name = m.group(1), m.group(2).strip()
            if sym in ("✓", "√"): passed.add(name)
            elif sym in ("✕", "✗", "×"): failed.add(name)
            else: skipped.add(name)
        passed -= failed; skipped -= failed; skipped -= passed
        return TestResult(passed_count=len(passed), failed_count=len(failed), skipped_count=len(skipped),
                          passed_tests=passed, failed_tests=failed, skipped_tests=skipped)
