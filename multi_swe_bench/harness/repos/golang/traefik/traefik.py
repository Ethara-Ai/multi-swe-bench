import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _strip_binary_diffs(patch: str) -> str:
    """Drop binary file sections from a unified diff.

    ``git apply`` is atomic: a single binary hunk lacking a full index line
    (``Binary files a/x and b/x differ`` or a ``GIT binary patch`` block)
    aborts the WHOLE apply, so with ``set -e`` in *-run.sh the fix stage
    yields zero results and the record is misclassified invalid. Splitting on
    the ``diff --git`` boundary and dropping only the binary sections lets the
    text hunks (the Go test/source changes that carry the f2p/n2p signal)
    apply cleanly.
    """
    if not patch:
        return patch
    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    kept = [
        s for s in sections
        if "Binary files " not in s and "GIT binary patch" not in s
    ]
    return "".join(kept)


class TraefikImageDefault(Image):
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
        # Generic traefik config for jsonl records that carry no number_interval
        # (so Instance.create resolves them to the plain "traefik/traefik" key).
        # The targeted PRs sit on the v1.x release lines (e.g. base ref "v1.6"),
        # which predate Go modules: github.com/containous import path, glide/dep
        # + vendor/, go-bindata for static assets. golang:1.13 builds these in
        # GOPATH mode.
        #
        # Returning a string lets the shared Image.dockerfile() own the build:
        # it clones "${REPO_URL}", checks out "${BASE_COMMIT}", runs
        # extra_setup(), and appends the _HARDENING_BLOCK that strips every other
        # ref/commit so the fix can't be read out of git history.
        # DockerfileEnhancer then injects the reference-format infra (syntax,
        # ARG TARGETARCH/REPO_URL/BASE_COMMIT, ethara LABEL) + final sanitize.
        # No proxy/cert.
        return "golang:1.13"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_apt_update_command(self, packages_str: str, base_img: str) -> str:
        # golang:1.13 is built on Debian buster, whose apt repos have moved to
        # archive.debian.org. The base image name ("golang:1.13") doesn't match
        # DEPRECATED_DEBIAN_IMAGES, so apt-get update would 404 against the live
        # mirrors. Force the archive-rewrite branch by handing the parent a
        # buster tag, which triggers the sources.list fixup before installing.
        return super()._get_apt_update_command(packages_str, "debian:buster")

    def extra_setup(self) -> str:
        # Runs after "git checkout ${BASE_COMMIT}" and before the hardening
        # block. These v1.x PRs predate Go modules, so they build under GOPATH:
        # the repo (cloned to /home/{repo}) is symlinked into the legacy import
        # path and go-bindata is installed for `go generate`. GO111MODULE=off /
        # GOPATH are set here so they persist into the eval container. The staged
        # helper scripts + patches live outside /home/{repo}, so the hardening
        # pass (which only touches the git tree) leaves them untouched.
        repo = self.pr.repo
        return (
            "ENV GO111MODULE=off\n"
            "ENV GOPATH=/go\n"
            f"RUN mkdir -p /go/src/github.com/containous && \\\n"
            f"    ln -sf /home/{repo} /go/src/github.com/containous/{repo}\n"
            "RUN go get -u github.com/jteeuwen/go-bindata/... || true\n"
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
                _strip_binary_diffs(self.pr.fix_patch),
            ),
            File(
                ".",
                "test.patch",
                _strip_binary_diffs(self.pr.test_patch),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# Repo is already cloned + checked out at ${{BASE_COMMIT}} and hardened by
# Image.dockerfile(); the GOPATH symlink is created in extra_setup(). This
# script only regenerates bindata and warms caches so the eval runs offline.
set -e

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
git reset --hard || true

go generate ./... 2>&1 || true
go build ./... 2>&1 || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

mkdir -p /go/src/github.com/containous
[ -e /go/src/github.com/containous/{pr.repo} ] || ln -sf /home/{pr.repo} /go/src/github.com/containous/{pr.repo}
cd /go/src/github.com/containous/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]


@Instance.register("traefik", "traefik")
class Traefik(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TraefikImageDefault(self.pr, self._config)

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
            index = test_name.rfind("/")
            if index == -1:
                return test_name
            return test_name[:index]

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

        # Go subtests share a base name (get_base_name strips "/sub"); if any
        # subtest failed, the base must not also remain in passed/skipped, or the
        # harness rejects the report ("passed and failed should not overlap").
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
