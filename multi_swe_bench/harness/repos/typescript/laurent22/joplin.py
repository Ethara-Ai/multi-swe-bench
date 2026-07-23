import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Remove binary diff hunks so `git apply` never leaves .rej / partially
    applies on a binary hunk (96 of joplin's 208 records carry them, mostly
    images / *.node / snapshot fixtures). Safe: binary hunks touch no TS source
    and never affect the unit test outcome."""
    import re as _re
    sections = _re.split(r"(?=^diff --git )", patch or "", flags=_re.MULTILINE)
    return "".join(
        s for s in sections
        if s and "Binary files " not in s and "GIT binary patch" not in s
    )



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
        # Shared base per era (built once, reused by every PR of that era). The
        # `# syntax` directive opts out of DockerfileEnhancer so this hand-written
        # layout is used verbatim: clone FULL history + light harden only. The
        # strict anti-reward-hack strip runs in the PR layer at each PR's base.sha.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            fetch = 'RUN git clone "${{REPO_URL}}" /home/{repo}'.format(repo=repo)
        else:
            fetch = "COPY {repo} /home/{repo}".format(repo=repo)

        return """# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
RUN sed -i 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' /etc/apt/sources.list || true && \\
    sed -i 's|http://security.debian.org|http://archive.debian.org|g' /etc/apt/sources.list || true && \\
    sed -i '/stretch-updates/d' /etc/apt/sources.list || true && \\
    sed -i '/buster-updates/d' /etc/apt/sources.list || true && \\
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until || true
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git jq build-essential python3 libsqlite3-dev libvips-dev rsync ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'
{fetch}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            org=org,
            repo=repo,
            fetch=fetch,
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
