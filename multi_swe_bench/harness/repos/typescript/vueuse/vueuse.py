"""vueuse/vueuse harness config, conformed to the hardened image.py.

Single per-PR image whose dependency() returns a *string* base image, so the
shared Image.dockerfile() owns the build: clone "${REPO_URL}", checkout
"${BASE_COMMIT}", run extra_setup(), then the _HARDENING_BLOCK that strips
every other ref/commit (the fix can't be read from git history).

DISPATCH IS BY RELEASE VERSION, NOT pr.number. These are release-window
bundles whose pr.number (first PR in the window) is NOT monotonic with the
release version (e.g. PR#4158 is v13.5 while PR#4349 is v11.3). The base.sha
is the window's START tag, so we parse the start version from base.label and
pick tooling from that. The tooling timeline (verified against the repo):

    v0.0.x  – v6.4    yarn  + jest
    v6.5    – v7.5.2  pnpm  + jest
    v7.5.3  – v12.2   pnpm  + vitest run
    v12.3+           pnpm  + vitest --project unit   (workspace split)

pnpm version drifts none/6/7/8/9/10 across releases, so we let corepack honor
the repo's own `packageManager` field (falling back to pnpm@7 for early
commits that predate it) instead of pinning a single version.
"""

from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# git-apply excludes shared by every eval script: never let a lockfile or a
# generated bundle in the patch disturb the working tree.
_APPLY_EXCLUDES = (
    "--exclude='*pnpm-lock.yaml' "
    "--exclude='*yarn.lock' "
    "--exclude='*package-lock.json'"
)

# Tooling-era boundaries, keyed on the base (start) release version.
_V_PNPM = (6, 5, 0)        # yarn -> pnpm
_V_VITEST = (7, 5, 3)      # jest -> vitest
_V_PROJECT = (12, 3, 0)    # vitest run -> vitest --project unit (workspace split)


def _start_version(label: str) -> tuple[int, int, int]:
    """Parse the start version from a base.label like 'v12.8.2..v13.0.0'."""
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", label or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def _runner(pr: PullRequest) -> str:
    """'jest' or 'vitest' for the base version — used by parse_log too."""
    return "jest" if _start_version(pr.base.label) < _V_VITEST else "vitest"


class VueuseImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    # --- era helpers ---------------------------------------------------
    def _era(self) -> str:
        v = _start_version(self.pr.base.label)
        if v < _V_PNPM:
            return "yarn"
        if v < _V_VITEST:
            return "pnpm_jest"
        if v < _V_PROJECT:
            return "pnpm_vitest"
        return "pnpm_vitest_project"

    def _test_cmd(self) -> str:
        era = self._era()
        if era in ("yarn", "pnpm_jest"):
            return "npx jest --verbose"
        if era == "pnpm_vitest":
            return "pnpm exec vitest run --reporter=verbose"
        return "pnpm exec vitest --project unit --run --reporter=verbose"

    def _pnpm_setup(self) -> str:
        # Honor the repo's declared packageManager (pnpm 6..10) via corepack so
        # the lockfileVersion matches; corepack downloads the pinned pnpm at
        # build time and caches it for the (offline) eval runs. Early commits
        # have no packageManager field -> pin pnpm@7.
        return (
            "if grep -q '\"packageManager\"' package.json 2>/dev/null; then\n"
            "  corepack enable || true\n"
            "else\n"
            "  npm install -g pnpm@7 || true\n"
            "fi"
        )

    # --- image plumbing ------------------------------------------------
    def dependency(self) -> Union[str, "Image"]:
        # A string base image hands the build to Image.dockerfile(), which
        # clones ${REPO_URL}, checks out ${BASE_COMMIT}, and hardens history.
        v = _start_version(self.pr.base.label)
        return "node:20" if v >= _V_PROJECT else "node:18"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return ["jq"]

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the helper scripts + patches into /home/ and runs
        # prepare.sh (package manager + deps + offline setup). node_modules
        # lives inside the repo but is untracked, so the hardening pass (which
        # only rewrites git history) leaves it intact.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN bash /home/prepare.sh"
        )

    def dockerfile(self) -> str:
        # Self-contained Dockerfile that emits the BuildKit syntax directive
        # up front. DockerfileEnhancer.enhance() returns the content unchanged
        # whenever that directive is present, so this registry NEVER receives
        # proxy / CA-cert / MITM injection regardless of the image.py in use.
        # We therefore declare the REPO_URL/BASE_COMMIT ARGs and the hardening
        # block ourselves so the build stays correct without the enhancer.
        base_img = self.dependency()
        if isinstance(base_img, Image):
            base_img = base_img.image_full_name()
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        default_packages = [
            "ca-certificates", "curl", "build-essential", "git",
            "gnupg", "make", "python3", "sudo", "wget",
        ]
        packages_str = " \\\n    ".join(default_packages + self.extra_packages())
        apt_command = self._get_apt_update_command(packages_str, base_img)

        sections = [
            "# syntax=docker/dockerfile:1.6",
            f"FROM {base_img}",
            (
                "ARG TARGETARCH\n"
                f'ARG REPO_URL="{repo_url}"\n'
                "ARG BASE_COMMIT"
            ),
            (
                "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                "    LANG=C.UTF-8 \\\n"
                "    TZ=UTC"
            ),
            (
                f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
                f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
                f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
                '      org.opencontainers.image.authors="https://www.ethara.ai/"'
            ),
            "WORKDIR /home/",
            apt_command,
            f'RUN git clone "${{REPO_URL}}" /home/{repo}',
            f"WORKDIR /home/{repo}",
            "RUN git reset --hard\nRUN git checkout ${BASE_COMMIT}",
            self.extra_setup(),
            Image._HARDENING_BLOCK,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(s for s in sections if s) + "\n"

    def _prepare_body(self) -> str:
        era = self._era()
        if era == "yarn":
            # yarn ships with the official node image. Very early vueuse (v0.0.x)
            # generates packages/api.ts via an interactive `node scripts/switch`;
            # the suites import "../api", so replicate it (Vue 2 = api.2.ts).
            return (
                "yarn install || true\n"
                "if [ -f packages/api.2.ts ] && [ ! -f packages/api.ts ]; then\n"
                "  cp packages/api.2.ts packages/api.ts\n"
                "fi"
            )
        # pnpm eras
        lines = [self._pnpm_setup(), "pnpm install --no-frozen-lockfile || true"]
        if era in ("pnpm_vitest", "pnpm_vitest_project"):
            # @vueuse/metadata/index.json is git-ignored and generated by the
            # root `update` script; without it metadata.ts fails to import and
            # test/exports.test.ts cannot load. Best-effort (no-op if absent).
            lines.append("pnpm run update >/dev/null 2>&1 || true")
        return "\n".join(lines)

    def files(self) -> list[File]:
        test_cmd = self._test_cmd()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script performs no git checkout. It installs the
# package manager + dependencies so the eval runs don't need network.
set -e

cd /home/{repo}
git reset --hard || true
{body}
""".format(repo=self.pr.repo, body=self._prepare_body()),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard HEAD || true
git apply {excludes} --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, excludes=_APPLY_EXCLUDES, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git reset --hard HEAD || true
git apply --3way {excludes} --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, excludes=_APPLY_EXCLUDES, test_cmd=test_cmd),
            ),
        ]


@Instance.register("vueuse", "vueuse")
class Vueuse(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VueuseImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Strip ANSI escape codes and null bytes for reliable matching
        ansi_re = re.compile(r"\x1b\[[0-9;]*m|\x00")

        if _runner(self.pr) == "jest":
            # Jest output format:
            #   PASS packages/core/useMemoize/index.test.ts
            #   FAIL packages/core/useBase64/index.test.ts
            re_pass_file = re.compile(r"PASS\s+(\S+\.(?:test|spec)\.tsx?)")
            re_fail_file = re.compile(r"FAIL\s+(\S+\.(?:test|spec)\.tsx?)")

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue
                m = re_pass_file.search(clean)
                if m:
                    passed_tests.add(m.group(1))
                    continue
                m = re_fail_file.search(clean)
                if m:
                    failed_tests.add(m.group(1))
        else:
            # Vitest verbose output format:
            #   ✓ packages/core/useMemoize/index.test.ts > ...
            #   ✓ unit packages/core/useMemoize/index.test.ts > ...   (workspace)
            #   ×/FAIL packages/core/... or FAIL unit packages/core/...
            re_pass = re.compile(r"[✓√]\s+(?:unit\s+)?(\S+\.(?:test|spec)\.tsx?)")
            re_fail = re.compile(
                r"(?:[×✗❯]|FAIL)\s+(?:unit\s+)?(\S+\.(?:test|spec)\.tsx?)"
            )

            for line in test_log.splitlines():
                clean = ansi_re.sub("", line).strip()
                if not clean:
                    continue
                m = re_pass.search(clean)
                if m:
                    passed_tests.add(m.group(1))
                    continue
                m = re_fail.search(clean)
                if m:
                    failed_tests.add(m.group(1))

        # A file with any failure is failed (not passed)
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
