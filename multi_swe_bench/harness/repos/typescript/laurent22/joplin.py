import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

"""


# ---------------------------------------------------------------------------
# Shared base image — parameterized by Node version
# ---------------------------------------------------------------------------


class JoplinImageBase(Image):
    """Base image for laurent22/joplin — clones repo, installs system deps.

    Args:
        node_image: Docker image name (e.g. "node:18", "node:16")
        interval_name: Used for image_tag/workdir dedup
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        node_image: str,
        interval_name: str,
    ):
        self._pr = pr
        self._config = config
        self._node_image = node_image
        self._interval_name = interval_name

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._node_image

    def image_tag(self) -> str:
        return "base-{name}".format(name=self._interval_name)

    def workdir(self) -> str:
        return "base-{name}".format(name=self._interval_name)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list || true && \
    sed -i 's|http://security.debian.org|http://archive.debian.org|g' /etc/apt/sources.list || true && \
    sed -i '/stretch-updates/d' /etc/apt/sources.list || true && \
    sed -i '/buster-updates/d' /etc/apt/sources.list || true && \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until || true
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git jq build-essential python3 libsqlite3-dev libvips-dev rsync \\
    && rm -rf /var/lib/apt/lists/*

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


# ---------------------------------------------------------------------------
# Shared parse_log for Jest output (used by Era 2 and Era 3)
# ---------------------------------------------------------------------------


def joplin_parse_log(test_log: str) -> TestResult:
    """Parse Jest test output for Joplin.

    Handles:
    - Lerna --stream output: `@joplin/lib: PASS path/to/test.js`
    - Yarn workspaces foreach --verbose output: `[@joplin/lib]: PASS path/to/test.js`
    - Plain Jest output: `PASS path/to/test.js`
    - Jest individual test pass/fail lines with checkmarks/crosses
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    # Lerna --stream prefix: `@joplin/lib: ` or `joplin: `
    # Yarn workspaces foreach --verbose prefix: `[@joplin/lib]: ` or `[joplin]: `
    re_pkg_prefix = re.compile(
        r"^(\[?@?[\w\-/]+\]?):\s*(.*)"
    )

    # Jest suite-level PASS/FAIL (file-level)
    re_jest_pass_suite = re.compile(
        r"^\s*PASS\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )
    re_jest_fail_suite = re.compile(
        r"^\s*FAIL\s+(.+?)(?:\s+\(\d+[\.\d]*\s*(?:ms|s)\))?$"
    )

    # Jest individual test pass/fail/skip
    re_jest_pass_test = re.compile(
        r"^\s*[✔✓√]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_jest_fail_test = re.compile(
        r"^\s*[×✕✗✘✖]\s+(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )
    re_jest_skip_test = re.compile(
        r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)(?:\s*\(\d+[\.\d]*\s*(?:ms|s)\))?\s*$"
    )

    # Jest fail indicator: ● Suite › Test name
    re_jest_fail_indicator = re.compile(r"^\s*●\s+(.+?)\s+›\s+(.+)$")

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        # Strip Yarn Berry verbose prefix: ➤ YN0000: ...
        line = re.sub(r"^➤ YN\d+:\s*", "", line)
        if not line:
            continue

        # Extract lerna/yarn workspace prefix for qualified test names
        pkg_prefix = ""
        pm = re_pkg_prefix.match(line)
        if pm:
            pkg_prefix = pm.group(1).strip("[]") + ": "
            line = pm.group(2).strip()

        # Jest suite-level PASS/FAIL
        m = re_jest_pass_suite.match(line)
        if m:
            passed_tests.add(pkg_prefix + m.group(1).strip())
            continue

        m = re_jest_fail_suite.match(line)
        if m:
            name = pkg_prefix + m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        # Jest individual test pass/fail/skip
        m = re_jest_pass_test.match(line)
        if m:
            name = pkg_prefix + m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_jest_fail_test.match(line)
        if m:
            name = pkg_prefix + m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_jest_skip_test.match(line)
        if m:
            skipped_tests.add(pkg_prefix + m.group(1).strip())
            continue

        # Jest fail indicator
        m = re_jest_fail_indicator.match(line)
        if m:
            name = "{prefix}{suite} > {test}".format(
                prefix=pkg_prefix, suite=m.group(1).strip(), test=m.group(2).strip()
            )
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )
