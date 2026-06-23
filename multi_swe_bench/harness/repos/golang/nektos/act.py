import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ActImageDefault(Image):
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
        # Returning a string (rather than a chained Image) lets the shared
        # Image.dockerfile() in image.py own the build: it clones "${REPO_URL}",
        # checks out "${BASE_COMMIT}", runs extra_setup(), and appends the
        # _HARDENING_BLOCK that strips every other ref/commit so the fix can't be
        # read out of git history. DockerfileEnhancer then injects the proxy/cert
        # infra and the final sanitize pass. None of that fires when dockerfile()
        # is overridden, which is why the previous two-stage build bypassed it.
        return "golang:latest"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # DockerfileEnhancer in image.py injects proxy / MITM / CA-cert
    # infrastructure into every generated Dockerfile. The ECR images for this
    # repo must NOT carry that MITM proxy / cert wiring, so we emit a
    # fully-formed Dockerfile here that carries the BuildKit syntax directive.
    # DockerfileEnhancer.enhance() returns the Dockerfile untouched when that
    # directive is already present, so its proxy ARGs, proxy ENV vars, CA-cert
    # symlinks and MITM secret mount are all skipped. We re-emit only the
    # non-proxy infra (REPO_URL / BASE_COMMIT ARGs + image labels + plain env).
    _SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"

    _ENV_BLOCK = (
        "ENV DEBIAN_FRONTEND=noninteractive \\\n"
        "    LANG=C.UTF-8 \\\n"
        "    TZ=UTC"
    )

    def dockerfile(self) -> str:
        raw = super().dockerfile()

        org = self.pr.org
        repo = self.pr.repo
        github_repo = repo[: -len("_root")] if repo.endswith("_root") else repo
        repo_url = f"https://github.com/{org}/{github_repo}.git"

        build_args = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="{repo_url}"\n'
            "ARG BASE_COMMIT"
        )
        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )
        infra = "\n\n".join([build_args, self._ENV_BLOCK, label_block]) + "\n"

        lines = raw.split("\n")
        from_idx = next(
            (i for i, l in enumerate(lines) if l.strip().upper().startswith("FROM ")),
            None,
        )
        if from_idx is None:
            return raw

        result = [self._SYNTAX_DIRECTIVE, ""]
        result.extend(lines[:from_idx])
        result.append(lines[from_idx].strip())
        result.append("")
        result.append(infra)
        result.extend(lines[from_idx + 1 :])
        return "\n".join(result)

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. Stages the runtime helper scripts + patches into /home/ and
        # warms the Go module cache so the eval scripts run offline. The copied
        # files live outside /home/{repo}, so the hardening pass (which only
        # operates inside the git tree) leaves them untouched.
        return (
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "COPY prepare.sh /home/prepare.sh\n"
            "RUN bash /home/prepare.sh"
        )

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
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(), so this script no longer performs any git checkout. It
# only warms the Go module/build caches so the eval runs don't need network.
set -e

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git reset --hard || true

go mod download -x 2>&1 || true
go build ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /home/{pr.repo}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]


@Instance.register("nektos", "act")
class Act(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ActImageDefault(self.pr, self._config)

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

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
