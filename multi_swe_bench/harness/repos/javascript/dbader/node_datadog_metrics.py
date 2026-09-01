import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TEST_CMD = "npm test 2>&1"


class NodeDatadogMetricsImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self):
        return self._pr

    @property
    def config(self):
        return self._config

    def dependency(self):
        return "node:20-bullseye"

    def image_tag(self):
        return "base"

    def workdir(self):
        return "base"

    def files(self):
        return []

    def dockerfile(self):
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
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates build-essential && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{self.clear_env}

"""


class NodeDatadogMetricsImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self):
        return self._pr

    @property
    def config(self):
        return self._config

    def dependency(self):
        return NodeDatadogMetricsImageBase(self.pr, self._config)

    def image_tag(self):
        return f"pr-{self.pr.number}"

    def workdir(self):
        return f"pr-{self.pr.number}"

    def files(self):
        reset = "git reset --hard HEAD >/dev/null 2>&1 || true; git clean -fdq -e node_modules >/dev/null 2>&1 || true"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "prepare.sh", """#!/bin/bash
set -e
cd /home/{pr.repo}
git reset --hard >/dev/null 2>&1 || true
if ! git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null; then
  git remote add origin https://github.com/{pr.org}/{pr.repo}.git 2>/dev/null \\
    || git remote set-url origin https://github.com/{pr.org}/{pr.repo}.git
  git fetch --no-tags --depth=1 origin {pr.base.sha}
fi
git checkout {pr.base.sha}
npm ci >/dev/null 2>&1 || npm install >/dev/null 2>&1 || true
""".format(pr=self.pr)),
            File(".", "run.sh", """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
{reset}
{cmd}
""".format(pr=self.pr, reset=reset, cmd=_TEST_CMD)),
            File(".", "test-run.sh", """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
{reset}
git apply --whitespace=nowarn /home/test.patch
{cmd}
""".format(pr=self.pr, reset=reset, cmd=_TEST_CMD)),
            File(".", "fix-run.sh", """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}
{reset}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch
npm install >/dev/null 2>&1 || true
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


@Instance.register("dbader", "node-datadog-metrics")
@Instance.register("dbader", "92")
@Instance.register("dbader", "100")
@Instance.register("dbader", "102")
@Instance.register("dbader", "128")
@Instance.register("dbader", "141")
class NodeDatadogMetrics(Instance):
    def __init__(self, pr, config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self):
        return self._pr

    def dependency(self):
        return NodeDatadogMetricsImageDefault(self.pr, self._config)

    def run(self, c=""):
        return c or "bash /home/run.sh"

    def test_patch_run(self, c=""):
        return c or "bash /home/test-run.sh"

    def fix_patch_run(self, c=""):
        return c or "bash /home/fix-run.sh"

    def parse_log(self, test_log):
        clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        pass_re    = re.compile(r"^(\s*)[✓✔√]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        fail_sym   = re.compile(r"^(\s*)[✕✖✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        fail_num   = re.compile(r"^(\s*)\d+\)\s+(.+?)\s*$")
        skip_re    = re.compile(r"^(\s*)[\-○]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        suite_re   = re.compile(r"^(\s+)([^\s✓✔✕✖✗√×○\-\d].*?)\s*$")
        summary_re = re.compile(r"^\s*\d+\s+(passing|failing|pending)")
        suite_stack: list[tuple[int, str]] = []
        in_summary = False
        for line in clean.splitlines():
            if summary_re.match(line):
                in_summary = True
                continue
            if in_summary:
                continue
            matched = False
            for regex, bucket in (
                (pass_re, passed),
                (fail_sym, failed),
                (fail_num, failed),
                (skip_re, skipped),
            ):
                m = regex.match(line)
                if m:
                    indent = len(m.group(1))
                    leaf = m.group(2).strip().rstrip(":")
                    while suite_stack and suite_stack[-1][0] >= indent:
                        suite_stack.pop()
                    full = " > ".join([n for _, n in suite_stack] + [leaf])
                    bucket.add(full)
                    matched = True
                    break
            if matched:
                continue
            s = suite_re.match(line)
            if s:
                indent = len(s.group(1))
                name = s.group(2).strip()
                while suite_stack and suite_stack[-1][0] >= indent:
                    suite_stack.pop()
                suite_stack.append((indent, name))
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
