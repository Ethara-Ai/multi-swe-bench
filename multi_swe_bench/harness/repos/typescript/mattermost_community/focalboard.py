from __future__ import annotations

import json
from typing import Optional, Union

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Markers wrapped around the machine-readable jest report inside the stage log.
# parse_log() reads ONLY what sits between them - never the human-readable
# tick-mark output, whose leaf titles collide and silently collapse f2p/p2p ids.
JSON_START = "-----MSB_JEST_JSON_START-----"
JSON_END = "-----MSB_JEST_JSON_END-----"

# Opt this image out of DockerfileEnhancer WITHOUT paying for a frontend image.
#
# enhance() bails out early on `if cls.SYNTAX_DIRECTIVE in raw` (image.py:317) -
# a plain substring test over the whole file. Docker, by contrast, only honours
# `# syntax=` as a parser directive when it appears in the LEADING comment block,
# before any instruction. Putting the marker AFTER the FROM therefore satisfies
# the enhancer while leaving Docker on its built-in dockerfile frontend.
#
# Why that matters: a real directive makes buildx resolve and pull
# docker.io/docker/dockerfile:1.6 on EVERY build, and multi-arch builds the base
# twice (once for the OCI export, once to --load the native platform). On
# 2026-09-03 that pull killed a 17-minute multi-arch run outright:
#
#     failed to fetch anonymous token: dial tcp: lookup auth.docker.io
#     on 10.255.255.254:53: i/o timeout
#
# after the arm64 and amd64 layers had already built successfully. It is also a
# Docker Hub rate-limit surface we do not need. This Dockerfile uses no BuildKit
# 1.6 syntax (no RUN --mount), so the built-in frontend is sufficient.
#
# If a `RUN --mount=...` is ever added here, this has to become a real leading
# directive again - and then the frontend pull comes back with it.
ENHANCER_OPT_OUT = (
    "# The next line is a MARKER, not a parser directive - it is deliberately not\n"
    "# the first line. It opts this file out of DockerfileEnhancer (image.py:317)\n"
    "# while leaving Docker on its built-in frontend. See focalboard.py for why.\n"
    f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}"
)

# ONE shared definition of the test invocation so run.sh, test-run.sh and
# fix-run.sh can never drift apart.
#
#   --ci            fail on a missing/changed snapshot instead of writing one
#   --coverage=false  package.json turns collectCoverage on; it only slows us down
#   --maxWorkers=2  deterministic worker count inside the container
#   --json          machine-readable report, written to a file (not stdout)
#   --forceExit     jsdom timers keep the process alive otherwise
JEST_RUN = """cd /home/{repo}/webapp

export CI=true

rm -f /home/jest_results.json

set +e
./node_modules/.bin/jest \\
    --ci \\
    --coverage=false \\
    --maxWorkers=2 \\
    --json \\
    --outputFile=/home/jest_results.json \\
    --forceExit
set -e

echo "{start}"
if [ -f /home/jest_results.json ]; then
    cat /home/jest_results.json
fi
echo ""
echo "{end}"
"""


def jest_run(repo: str) -> str:
    return JEST_RUN.format(repo=repo, start=JSON_START, end=JSON_END)


class FocalboardImageBase(Image):
    """Shared base for every PR in this dataset.

    Rule 9 shape: the base stops at the clone. It carries the toolchain, the
    infrastructure block and the clone, then CMD - no checkout, no pin, no gc,
    no scrub, no asserts. All of that lives in the pr-<N> layer.

    The `# syntax` directive is load-bearing: DockerfileEnhancer.enhance()
    returns the Dockerfile verbatim when it is present. Without it,
    _inject_final_sanitize() would append the hardening block to this image
    (it fires on any content holding `git clone` plus a CMD), which is exactly
    what rule 9 forbids here.
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

    def dependency(self) -> Union[str, "Image"]:
        # focalboard CI used node-version 16.1.0 in 2021 (.github/workflows).
        # The webapp pins jest 26.6.3 / ts-jest 26.5.4 / typescript 4.2.3, none
        # of which install cleanly on node 20.
        return "node:16-bullseye"

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

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        build_args = (
            f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
            f'ARG REPO_URL="{repo_url}"\n'
            f"ARG BASE_COMMIT\n"
            f"\n{DockerfileEnhancer._PROXY_ARGS}"
        )

        label_block = "LABEL " + " \\\n      ".join(
            [
                f'org.opencontainers.image.title="{org}/{repo}"',
                f'org.opencontainers.image.description="{org}/{repo} Docker image"',
                f'org.opencontainers.image.source="https://github.com/{org}/{repo}"',
                'org.opencontainers.image.authors="https://www.ethara.ai/"',
            ]
        )

        sections = [
            f"FROM {image_name}",
            ENHANCER_OPT_OUT,
            build_args,
            DockerfileEnhancer._ENV_BLOCK,
            label_block,
            DockerfileEnhancer._CERT_SYMLINKS,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        sections.append(f"WORKDIR /home/{repo}")

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class FocalboardImageDefault(Image):
    """Per-PR image.

    Rule 9 shape: FROM the shared base, the seven COPY lines, the hardcoded
    ARG BASE_COMMIT, `RUN bash /home/prepare.sh`, then the FULL hardening block
    last. The scrub has to come after prepare.sh because npm needs the network,
    and it has to carry its own ARG because build_dataset.py only passes
    REPO_URL / BASE_COMMIT as build args to string-dependency() images.
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

    def dependency(self) -> Image | None:
        return FocalboardImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

# Force a real content comparison. A stat-cache hit can make `git status`
# report a clean tree that is not actually clean.
git update-index -q --really-refresh || true

if ! git diff --quiet; then
  echo "check_git_changes: Unstaged content differences"
  exit 1
fi

if ! git diff --cached --quiet; then
  echo "check_git_changes: Staged content differences"
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

cd /home/{pr.repo}/webapp

# --ignore-scripts skips the postinstall downloads (cypress binary, the
# imagemin-* native helpers). None of them is reachable from a jest run, and
# every one of them is a network fetch that can fail the build.
npm ci --ignore-scripts --no-audit --no-fund \\
    || npm install --ignore-scripts --no-audit --no-fund

# node_modules is gitignored, so the tree must still be clean here.
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

{jest}
""".format(pr=self.pr, jest=jest_run(self.pr.repo)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

{jest}
""".format(pr=self.pr, jest=jest_run(self.pr.repo)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{jest}
""".format(pr=self.pr, jest=jest_run(self.pr.repo)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

{self._HARDENING_BLOCK}"""


@Instance.register("mattermost-community", "focalboard")
class Focalboard(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FocalboardImageDefault(self.pr, self._config)

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

        report = self._extract_report(test_log)
        if report is None:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )

        prefix = f"/home/{self.pr.repo}/"

        for suite in report.get("testResults") or []:
            path = suite.get("name") or ""
            if path.startswith(prefix):
                rel_path = path[len(prefix):]
            else:
                rel_path = path.lstrip("/")

            assertions = suite.get("assertionResults") or []

            # A suite that never produced an assertion (type error, import
            # failure, out-of-memory) must still show up as a failure, or the
            # whole file silently vanishes from both f2p and p2p.
            if not assertions:
                if (suite.get("status") or "").lower() == "failed":
                    failed_tests.add(f"jest::{rel_path}::<suite failed to run>")
                continue

            for assertion in assertions:
                name = assertion.get("fullName") or assertion.get("title") or ""
                if not name:
                    continue

                # Test id shape is <tool>::<path>::<name>. The tool has to come
                # first: with <path>::<name>, report.py rejects the instance
                # whenever the fix patch creates that file.
                test_id = f"jest::{rel_path}::{name}"
                status = (assertion.get("status") or "").lower()

                if status == "passed":
                    passed_tests.add(test_id)
                elif status == "failed":
                    failed_tests.add(test_id)
                else:
                    # pending / todo / skipped / disabled
                    skipped_tests.add(test_id)

        # Failure always wins over a duplicate pass or skip.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    @staticmethod
    def _extract_report(test_log: str) -> Optional[dict]:
        start = test_log.rfind(JSON_START)
        if start == -1:
            return None

        start += len(JSON_START)
        end = test_log.find(JSON_END, start)
        if end == -1:
            return None

        blob = test_log[start:end].strip()
        if not blob:
            return None

        try:
            return json.loads(blob)
        except ValueError:
            return None
