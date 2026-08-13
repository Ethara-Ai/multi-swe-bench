import json as _json
import re
from typing import Optional

from multi_swe_bench.harness import pull_request as _pull_request
from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Bundle records ship with a `prs_in_bundle` list and an empty `number_interval`.
# Required OUTPUT format is the dash-joined bundle list (e.g. [146,147,150] ->
# "146-147-150"), NEVER a range like "146-150" (a range would wrongly imply
# every intermediate PR is included). Instance.create uses `number_interval` as
# a routing key when non-empty, and only `gsd-build/get-shit-done` is registered
# here, so we must NOT set pr.number_interval before build/routing. Instead we
# stash the dash-joined value on a non-field attr and copy it onto the OUTPUT
# Dataset row inside Dataset.build. Same pattern as ytdl-org/youtube-dl.
if not getattr(_pull_request.PullRequest, "_gsd_number_interval_patched", False):
    _gsd_orig_from_json = _pull_request.PullRequest.from_json.__func__

    def _gsd_from_json(cls, json_str):
        pr = _gsd_orig_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
            if (
                raw.get("org") == "gsd-build"
                and raw.get("repo") == "get-shit-done"
                and raw.get("prs_in_bundle")
            ):
                pr._gsd_number_interval = "-".join(
                    str(p) for p in raw["prs_in_bundle"]
                )
        except Exception:
            pass
        return pr

    _pull_request.PullRequest.from_json = classmethod(_gsd_from_json)
    _pull_request.PullRequest._gsd_number_interval_patched = True

    from multi_swe_bench.harness.dataset import Dataset as _Dataset

    if not _Dataset.__dict__.get("_gsd_build_patched", False):
        _gsd_orig_build = _Dataset.build.__func__

        def _gsd_build(cls, pr, report):
            ds = _gsd_orig_build(cls, pr, report)
            ni = getattr(pr, "_gsd_number_interval", "")
            if ni:
                ds.number_interval = ni
            return ds

        _Dataset.build = classmethod(_gsd_build)
        _Dataset._gsd_build_patched = True


class GsdBuildImageBase(Image):
    # Shared base image consumed by every per-PR GsdBuildImageDefault. Carries
    # the expensive one-time steps: apt install + git clone + warm npm install.
    # Emits tag ":base". All 28 bundle rows produce instances that hash to the
    # same image_full_name(), so the harness dependency graph builds it exactly
    # once (Image.__hash__ / __eq__ key off image_full_name -- image.py:89-95).
    # Embeds SYNTAX_DIRECTIVE at line 1 so DockerfileEnhancer.enhance()
    # short-circuits at image.py:317-318 -- skips proxy/cert/MITM injection.
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
        return "node:22"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo
        github_repo = _safe_path_component(github_repo)
        repo_url = f"https://github.com/{org}/{github_repo}.git"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}
FROM node:22

ARG TARGETARCH
ARG REPO_URL="{repo_url}"

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} base image (shared across PR bundles)" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN npm install || true

CMD ["/bin/bash"]
"""


class GsdBuildImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> "GsdBuildImageBase":
        # Returning an Image (not a str) means DockerfileEnhancer.enhance()
        # short-circuits at image.py:315-316 -- skips proxy/cert/MITM injection.
        return GsdBuildImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        # FROM base (already has clone + warm install) -> checkout this PR's
        # BASE_COMMIT -> copy patches/scripts -> prepare.sh (refreshes npm for
        # this commit's package.json) -> _HARDENING_BLOCK -> CMD. Hardening runs
        # AFTER prepare.sh to match single-tier ordering (image.py:243-248).
        org = _safe_path_component(self.pr.org, "org")
        repo = _safe_path_component(self.pr.repo)
        base_full = self.dependency().image_full_name()
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}
FROM {base_full}

ARG TARGETARCH
ARG BASE_COMMIT="{self.pr.base.sha}"

LABEL org.opencontainers.image.title="{org}/{repo}#{self.pr.number}" \\
      org.opencontainers.image.description="{org}/{repo} PR #{self.pr.number} image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

COPY fix.patch /home/fix.patch
COPY test.patch /home/test.patch
COPY run.sh /home/run.sh
COPY test-run.sh /home/test-run.sh
COPY fix-run.sh /home/fix-run.sh
COPY prepare.sh /home/prepare.sh
RUN bash /home/prepare.sh

{hardening}

CMD ["/bin/bash"]
"""

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
                "prepare.sh",
                """#!/bin/bash
# Warm the npm install at image-build time so the eval runs don't need
# network. The repo is already checked out at ${{BASE_COMMIT}} and hardened
# by Image.dockerfile(), so this script no longer performs any git checkout
# itself. `npm install` is allowed to fail (|| true) because its only purpose
# here is to populate node_modules; the real pass/fail signal comes from the
# run/test-run/fix-run scripts.
set -e

cd /home/{pr.repo}
git reset --hard || true

npm install || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
elif [ -f get-shit-done/bin/gsd-tools.test.js ]; then
    node --test get-shit-done/bin/gsd-tools.test.js
elif [ -f package.json ] && grep -q '"vitest"' package.json; then
    npx --no-install vitest run --reporter=tap-flat 2>&1 | awk '/^(ok|not ok) [0-9]+/ {{print "    " $0; next}} {{print}}'
else
    echo "No known test runner found" >&2; exit 1
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git checkout HEAD -- package-lock.json 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
elif [ -f get-shit-done/bin/gsd-tools.test.js ]; then
    node --test get-shit-done/bin/gsd-tools.test.js
elif [ -f package.json ] && grep -q '"vitest"' package.json; then
    npx --no-install vitest run --reporter=tap-flat 2>&1 | awk '/^(ok|not ok) [0-9]+/ {{print "    " $0; next}} {{print}}'
else
    echo "No known test runner found" >&2; exit 1
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git checkout HEAD -- package-lock.json 2>/dev/null || true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f scripts/run-tests.cjs ]; then
    node scripts/run-tests.cjs
elif ls tests/*.test.cjs 1>/dev/null 2>&1; then
    node --test tests/*.test.cjs
elif [ -f get-shit-done/bin/gsd-tools.test.js ]; then
    node --test get-shit-done/bin/gsd-tools.test.js
elif [ -f package.json ] && grep -q '"vitest"' package.json; then
    npx --no-install vitest run --reporter=tap-flat 2>&1 | awk '/^(ok|not ok) [0-9]+/ {{print "    " $0; next}} {{print}}'
else
    echo "No known test runner found" >&2; exit 1
fi

""".format(pr=self.pr),
            ),
        ]


@Instance.register("gsd-build", "get-shit-done")
class GsdBuildGetShitDone(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GsdBuildImageDefault(self.pr, self._config)

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
        """Parse TAP v13 from ``node --test``.  Subtests qualified as
        ``suiteName > testName`` to deduplicate across suites."""
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        current_suite: str = ""
        re_suite = re.compile(r"^# Subtest: (.+)$")
        re_subtest_pass = re.compile(r"^\s+ok \d+ - (.+?)(?:\s+#.*)?$")
        re_subtest_fail = re.compile(r"^\s+not ok \d+ - (.+?)(?:\s+#.*)?$")
        re_skip = re.compile(r"#\s*(?:SKIP|skip|TODO|todo)")

        for line in test_log.splitlines():
            suite_match = re_suite.match(line)
            if suite_match:
                current_suite = suite_match.group(1)
                continue

            m = re_subtest_pass.match(line)
            if m:
                name = m.group(1)
                qualified = f"{current_suite} > {name}" if current_suite else name
                if re_skip.search(line):
                    skipped_tests.add(qualified)
                else:
                    passed_tests.add(qualified)
                continue

            m = re_subtest_fail.match(line)
            if m:
                name = m.group(1)
                qualified = f"{current_suite} > {name}" if current_suite else name
                if re_skip.search(line):
                    skipped_tests.add(qualified)
                else:
                    failed_tests.add(qualified)
                continue

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )



