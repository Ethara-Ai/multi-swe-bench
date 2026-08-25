"""mswjs/msw registry — two-level build with explicit hardening.

Base images (ImageBaseNode18/22/20) install the toolchain (node + APT packages +
corepack) and carry the shared `git clone "${REPO_URL}"` plus the light base
hardening required by PIPELINE.md 3.  Per-PR images (ImageDefault) inherit that
clone, COPY the patches and scripts, run prepare.sh (which resets and checks out
the PR's base commit), then apply the strict hardening block with the commit hash
hardcoded directly in the generated Dockerfile (PIPELINE.md 4).

Because ImageDefault.dependency() returns an Image (not a str), DockerfileEnhancer
bails and does NOT inject infra ARGs or hardening.  Both are handled manually:
  - dockerfile() writes "# syntax=docker/dockerfile:1.6" as the first line,
    which also prevents DockerfileEnhancer from running.
  - Image._HARDENING_BLOCK is embedded with ${BASE_COMMIT} substituted inline.

Era boundaries (by PR number)
  Group 1: PRs  ≤607      -- yarn + jest --runInBand     (node:18) → ImageBaseNode18
  Group 2: PRs 608-1375   -- yarn + jest --maxWorkers=3  (node:18) → ImageBaseNode18
  Group 3: PRs 1376-1436  -- pnpm + jest --maxWorkers=3  (node:22) → ImageBaseNode22
  Group 4: PRs 1437-2578  -- pnpm + vitest run           (node:18) → ImageBaseNode18
  Group 5: PRs 2579+      -- pnpm + vitest run           (node:20) → ImageBaseNode20
"""

import re
from types import SimpleNamespace
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# Shared script fragments
# ---------------------------------------------------------------------------

_TESTD_TSC_BLOCK = """
set +e

TEST_D_FILES=$(find src -name '*.test-d.ts' 2>/dev/null || true)
if [ -n "$TEST_D_FILES" ]; then
  TSC_OUT=$(npx tsc --noEmit --strict 2>&1 || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "^$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi
"""

_VITEST_EXTRA_BLOCK = """
set +e

VITEST_NODE_CONFIG=$(ls test/node/vitest.config.ts test/node/vitest.config.mts 2>/dev/null | head -1)
if [ -n "$VITEST_NODE_CONFIG" ]; then
  pnpm vitest run --config="$VITEST_NODE_CONFIG"
fi

if node -e "var p=require('./package.json'); process.exit(p.scripts && p.scripts['test:ts'] ? 0 : 1)" 2>/dev/null; then
  TSC_OUT=$(pnpm test:ts run 2>&1 || true)
  TEST_D_FILES=$(find src test -name '*.test-d.ts' 2>/dev/null || true)
  for f in $TEST_D_FILES; do
    if echo "$TSC_OUT" | grep -q "$f("; then
      echo "FAIL $f"
    else
      echo "PASS $f"
    fi
  done
fi
"""

_LOCKFILE_EXCLUDE = "--exclude yarn.lock --exclude pnpm-lock.yaml"


def _group_for(number: int) -> int:
    """Route a PR number to its toolchain era."""
    if number <= 607:
        return 1  # yarn + jest --runInBand     (node:18)
    elif number <= 1375:
        return 2  # yarn + jest --maxWorkers=3  (node:18)
    elif number <= 1436:
        return 3  # pnpm + jest --maxWorkers=3  (node:22)
    elif number <= 2578:
        return 4  # pnpm + vitest run           (node:18)
    else:
        return 5  # pnpm + vitest run           (node:20)


# ---------------------------------------------------------------------------
# Base image classes
# ---------------------------------------------------------------------------

def _base_proxy_pr():
    """Minimal PR-like object for base images.

    build_dataset.py accesses image.pr.org, image.pr.repo, and image.pr.base.sha
    unconditionally when dependency() returns a str.  Base images declare ARG
    REPO_URL / ARG BASE_COMMIT in their Dockerfile but never use them, so
    passing these dummy values as build-args is harmless.
    """
    base = SimpleNamespace(sha="")
    return SimpleNamespace(org="mswjs", repo="msw", base=base)


_BASE_DOCKERFILE_TEMPLATE = """\
# syntax=docker/dockerfile:1.6

FROM {node_version}

ARG TARGETARCH
ARG REPO_URL="https://github.com/mswjs/msw.git"
ARG BASE_COMMIT

{proxy_args}

{env_block}
ENV LC_ALL=C.UTF-8

LABEL org.opencontainers.image.title="mswjs/msw" \\
      org.opencontainers.image.description="mswjs/msw Docker image" \\
      org.opencontainers.image.source="https://github.com/mswjs/msw" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{cert_symlinks}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

CMD ["/bin/bash"]
"""


class _ImageBase(Image):
    """Abstract toolchain base image.  Concrete subclasses set _node_version/_tag."""

    _node_version: str
    _tag: str

    def __init__(self, config: Config):
        self._config = config
        self._pr_proxy = _base_proxy_pr()

    @property
    def pr(self):
        return self._pr_proxy

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return self._node_version

    def image_name(self) -> str:
        return "mswebench/mswjs_m_msw"

    def image_tag(self) -> str:
        return self._tag

    def workdir(self) -> str:
        return self._tag

    def files(self):
        return []

    def dockerfile(self) -> str:
        # MITM proxy/cert scaffolding is pulled straight from image.py so the
        # rendered Dockerfile matches the canonical constants verbatim
        # (PIPELINE.md 2a / 8.1).  This repo opts out of DockerfileEnhancer
        # (# syntax directive), so the block is applied here by hand.
        return _BASE_DOCKERFILE_TEMPLATE.format(
            node_version=self._node_version,
            repo=self.pr.repo,
            proxy_args=DockerfileEnhancer._PROXY_ARGS,
            env_block=DockerfileEnhancer._ENV_BLOCK,
            cert_symlinks=DockerfileEnhancer._CERT_SYMLINKS,
        )


class ImageBaseNode18(_ImageBase):
    _node_version = "node:18"
    _tag = "base-node18"


class ImageBaseNode22(_ImageBase):
    _node_version = "node:22"
    _tag = "base-node22"


class ImageBaseNode20(_ImageBase):
    _node_version = "node:20"
    _tag = "base-node20"


# ---------------------------------------------------------------------------
# Per-PR image
# ---------------------------------------------------------------------------

class ImageDefault(Image):
    """Per-PR image built on top of the appropriate toolchain base.

    dependency() returns an Image, so DockerfileEnhancer bails.
    dockerfile() is overridden to include the full build sequence with
    hardening applied using the PR's base commit hash.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def _group(self) -> int:
        return _group_for(self.pr.number)

    def dependency(self) -> Image:
        if self._group() == 3:
            return ImageBaseNode22(self._config)
        if self._group() == 5:
            return ImageBaseNode20(self._config)
        return ImageBaseNode18(self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # --- era helpers -------------------------------------------------------

    def _pm_setup(self) -> str:
        if self._group() <= 2:
            return "corepack enable\ncorepack prepare yarn@1.22.22 --activate"
        if self._group() == 3:
            return "corepack enable\ncorepack prepare pnpm@9.15.4 --activate"
        return "corepack enable"

    def _install_cmd(self) -> str:
        if self._group() <= 2:
            return "yarn install --frozen-lockfile || true"
        return "pnpm install --no-frozen-lockfile || true"

    def _reinstall_cmd(self) -> str:
        if self._group() <= 2:
            return "yarn install || true"
        return "pnpm install --no-frozen-lockfile || true"

    def _build_cmd(self) -> str:
        return "pnpm build || true" if self._group() >= 4 else ""

    def _runner_block(self) -> str:
        g = self._group()
        if g == 1:
            return "yarn jest --runInBand\n" + _TESTD_TSC_BLOCK
        elif g == 2:
            return "yarn jest --maxWorkers=3\n"
        elif g == 3:
            return "pnpm jest --maxWorkers=3\n"
        else:
            return "pnpm vitest run\n" + _VITEST_EXTRA_BLOCK

    # --- dockerfile glue ---------------------------------------------------

    def extra_setup(self) -> str:
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY check_git_changes.sh /home/check_git_changes.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def dockerfile(self) -> str:
        repo = self.pr.repo
        org = self.pr.org
        commit = self.pr.base.sha
        base_full = self.dependency().image_full_name()

        # Substitute the hardcoded commit hash into the hardening block so that
        # ARG BASE_COMMIT is not needed (build_dataset.py does not pass it when
        # dependency() returns an Image).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", commit)

        extra = self.extra_setup()

        return "\n".join([
            "# syntax=docker/dockerfile:1.6",
            "",
            f"FROM {base_full}",
            "",
            self.global_env,
            "",
            extra,
            "",
            f"WORKDIR /home/{repo}",
            "",
            hardening,
            self.clear_env,
            'CMD ["/bin/bash"]',
            "",
        ])

    def files(self) -> list[File]:
        repo = self.pr.repo
        pm_setup = self._pm_setup()
        install_cmd = self._install_cmd()
        reinstall_cmd = self._reinstall_cmd()
        build_cmd = self._build_cmd()
        runner_block = self._runner_block()

        build_line = f"{build_cmd}\n" if build_cmd else ""
        prepare = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            f"git checkout {self.pr.base.sha}\n"
            f"{pm_setup}\n"
            f"{install_cmd}\n"
            f"{build_line}"
        )

        run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"{runner_block}"
        )

        reinstall_line = f"{reinstall_cmd}\n"
        build_after_patch = f"{build_cmd}\n" if build_cmd else ""

        test_run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/test.patch || true\n"
            f"{reinstall_line}"
            f"{build_after_patch}"
            f"{runner_block}"
        )

        fix_run = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            f"cd /home/{repo}\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/test.patch || true\n"
            f"git apply {_LOCKFILE_EXCLUDE} --whitespace=nowarn --reject /home/fix.patch || true\n"
            f"{reinstall_line}"
            f"{build_after_patch}"
            f"{runner_block}"
        )

        check_git_changes = (
            "#!/bin/bash\n"
            "set -e\n"
            "\n"
            'if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n'
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "\n"
            'if [[ -n $(git status --porcelain) ]]; then\n'
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  exit 1\n"
            "fi\n"
            "\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------

@Instance.register("mswjs", "1096")
class Msw(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def _group(self) -> int:
        return _group_for(self.pr.number)

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        re_tsc_error = re.compile(r"(\S+\.test-d\.(?:tsx|ts))\(\d+,\d+\):\s*error\s+TS\d+")

        group = self._group()

        if group <= 3:
            re_pass = re.compile(r"PASS\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"FAIL\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        else:
            re_pass = re.compile(r"(?:PASS|[✓✔])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_fail = re.compile(r"(?:FAIL|[❯×✗])\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")
            re_skip = re.compile(r"[↓⊘]\s+(\S+\.(?:test-d|test|spec)\.(?:tsx|ts|jsx|js))")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue

                pass_match = re_pass.search(clean)
                if pass_match:
                    passed_tests.add(pass_match.group(1))
                    continue

                fail_match = re_fail.search(clean)
                if fail_match:
                    failed_tests.add(fail_match.group(1))
                    continue

                skip_match = re_skip.search(clean)
                if skip_match:
                    skipped_tests.add(skip_match.group(1))
                    continue

                tsc_match = re_tsc_error.search(clean)
                if tsc_match:
                    failed_tests.add(tsc_match.group(1))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# Bundle-level era registrations
# ---------------------------------------------------------------------------
_BUNDLE_INTERVALS = [
    "121-124",
    "141-143",
    "163-166-167-168",
    "179-182-183",
    "194-195",
    "198-201-204",
    "467-471",
    "774-843",
    "835-836-837-839-840",
    "1029-1050-1057-1061-1062-1063-1064",
    "1096-1098",
    "1155-1157-1159-1160-1161",
    "1257-1265",
    "1323-1369-1375",
    "1443-1815-1855-1857-1858-1861-1862",
    "1824-1825-1833",
    "1850-1853",
    "1957-1961",
    "1979-1993-1995-1997-1998-1999",
    "1988-1990",
    "1996-2000-2004-2020-2021-2031",
    "2002-2008",
    "2093-2094",
    "2108-2206",
    "2135-2144",
    "2349-2350",
    "2353-2354",
    "2677-2679",
]
for _interval in _BUNDLE_INTERVALS:
    Instance.register("mswjs", _interval)(Msw)
