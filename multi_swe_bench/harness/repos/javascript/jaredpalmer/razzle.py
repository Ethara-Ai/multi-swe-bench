import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# jaredpalmer/razzle#1693 "Add development build feature" adds a new example app
# (examples/with-development-build/) with a smoke test App.test.js (ReactDOM.render(<App/>)).
# razzle is a yarn-workspaces + lerna monorepo (jest 26). The example test isn't runnable by the
# repo's own jest.examples.config out of the box, so we scaffold a minimal jest+babel setup
# (validated empirically):
#   - @babel/core@7 (CJS; babel-jest 26 require()s it -- @babel/core@8 is ESM-only -> ERR_REQUIRE_ESM)
#     + preset-env + preset-react for JSX; react/react-dom (the example's deps aren't hoisted since
#     its package.json is added by the fix, after yarn install ran); identity-obj-proxy for CSS.
#   - jest config scoped to the one example (rootDir=repo root; scoped roots avoids haste collisions).
#   - git apply --binary --exclude='*.ico' (the fix adds a binary favicon that blocks a plain apply).

_SCAFFOLD_DEPS = ('"@babel/core@^7.20" "@babel/preset-env@^7.20" "@babel/preset-react@^7.18" '
                  "identity-obj-proxy react@17 react-dom@17")
_WRITE_CFG = ("echo 'bW9kdWxlLmV4cG9ydHMgPSB7IHByZXNldHM6IFsiQGJhYmVsL3ByZXNldC1lbnYiLCAiQGJhYmVsL3ByZXNldC1yZWFjdCJdIH07Cg==' | base64 -d > babel.config.js; "
              "echo 'bW9kdWxlLmV4cG9ydHMgPSB7IHJvb3REaXI6ICIuIiwgcm9vdHM6IFsiPHJvb3REaXI+L2V4YW1wbGVzL3dpdGgtZGV2ZWxvcG1lbnQtYnVpbGQiXSwgdGVzdE1hdGNoOiBbIjxyb290RGlyPi9leGFtcGxlcy93aXRoLWRldmVsb3BtZW50LWJ1aWxkLyoqLyooKi4pQChzcGVjfHRlc3QpLihqc3xqc3gpIl0sIG1vZHVsZU5hbWVNYXBwZXI6IHsgIlxcLihjc3N8bGVzc3xzY3NzKSQiOiAiaWRlbnRpdHktb2JqLXByb3h5IiB9IH07Cg==' | base64 -d > jest.rcd.config.js")
_RESET = ("git reset --hard HEAD >/dev/null 2>&1 || true; "
          "git clean -fdq -e babel.config.js -e jest.rcd.config.js -e node_modules >/dev/null 2>&1 || true")
_APPLY = "git apply --whitespace=nowarn --binary --exclude='*.ico'"
_TEST_CMD = "npx jest --config jest.rcd.config.js --verbose 2>&1"


class RazzleImageBase(Image):
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
RUN npm install --no-save --legacy-peer-deps {_SCAFFOLD_DEPS} || true
RUN {_WRITE_CFG}

{self.clear_env}

"""


class RazzleImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self): return self._pr
    @property
    def config(self): return self._config

    def dependency(self): return RazzleImageBase(self.pr, self._config)
    def image_prefix(self): return "envagent"
    def image_tag(self): return f"pr-{self.pr.number}"
    def workdir(self): return f"pr-{self.pr.number}"

    def files(self):
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
{writecfg}
{cmd}
""".format(pr=self.pr, reset=_RESET, writecfg=_WRITE_CFG, cmd=_TEST_CMD)),
            File(".", "test-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{reset}
{writecfg}
{apply} /home/test.patch || true
{cmd}
""".format(pr=self.pr, reset=_RESET, writecfg=_WRITE_CFG, apply=_APPLY, cmd=_TEST_CMD)),
            File(".", "fix-run.sh", """#!/bin/bash
set -eo pipefail
cd /home/{pr.repo}
{reset}
{writecfg}
{apply} /home/test.patch /home/fix.patch || true
{cmd}
""".format(pr=self.pr, reset=_RESET, writecfg=_WRITE_CFG, apply=_APPLY, cmd=_TEST_CMD)),
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


@Instance.register("jaredpalmer", "razzle")
class Razzle(Instance):
    def __init__(self, pr, config, *args, **kwargs):
        super().__init__(); self._pr = pr; self._config = config
    @property
    def pr(self): return self._pr
    def dependency(self): return RazzleImageDefault(self.pr, self._config)
    def run(self, c=""): return c or "bash /home/run.sh"
    def test_patch_run(self, c=""): return c or "bash /home/test-run.sh"
    def fix_patch_run(self, c=""): return c or "bash /home/fix-run.sh"
    def parse_log(self, test_log):
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed=set(); failed=set(); skipped=set()
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
