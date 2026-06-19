import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GoZeroImageBase(Image):
    """ONE shared toolchain base image (golang + build deps). Tag ``:base``.

    The harness BUILDS this once and every PR builds ``FROM`` it. It deliberately
    does NOT clone the repo and does NOT check out any commit, so a single
    ``:base`` serves every PR (a built image that checked out a commit would be
    frozen to it). The per-PR clone + checkout + hardening happen in
    ``GoZeroImageDefault``.

    ``dependency()`` returns the string ``"golang:latest"`` so the harness builds
    this image; with NO ``git clone`` line, DockerfileEnhancer adds only the infra
    ARGs/ENV/LABEL (proxy/cert injection is OFF, removed from image.py) and never
    hardens/pins the shared base.
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
        return "golang:latest"

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

        # Toolchain only: NO git clone here, so DockerfileEnhancer adds only its
        # infra block and never hardens or pins this shared base to a commit.
        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
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

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            clear_env=self.clear_env,
        )


class GoZeroImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        # Chain to the ONE shared :base. Because this is an Image (not a string),
        # DockerfileEnhancer returns dockerfile() verbatim, so we clone, check out
        # THIS PR's ${BASE_COMMIT}, stage the 6 eval files, warm caches, and apply
        # the hardening block manually — the per-PR work the shared base can't do.
        return GoZeroImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
# Warm the Go module + build caches so the eval runs don't need network. The repo
# is already checked out at ${{BASE_COMMIT}} (hardening runs right after this
# script), so prepare does no git checkout itself. `go test` is allowed to fail
# (|| true): it only populates caches; the real pass/fail signal comes from
# run/test-run/fix-run.
set -e

cd /home/{pr.repo}
git reset --hard || true

go mod download || true
go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
go mod tidy
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude='doc/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.pdf' /home/test.patch
go mod tidy
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --exclude='doc/*' --exclude='*.png' --exclude='*.jpg' --exclude='*.pdf' /home/test.patch /home/fix.patch
go mod tidy
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Per-PR work on the shared :base: clone, check out THIS PR's ${BASE_COMMIT},
        # stage scripts/patches, warm caches, then harden. REPO_URL + BASE_COMMIT are
        # baked as ARG defaults because this chains to a base *Image*, so
        # DockerfileEnhancer returns this verbatim and supplies neither.
        return """FROM {name}:{tag}

ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT="{base_sha}"

{global_env}

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}

{clear_env}

CMD ["/bin/bash"]
""".format(
            name=name,
            tag=tag,
            org=self.pr.org,
            repo=self.pr.repo,
            base_sha=self.pr.base.sha,
            global_env=self.global_env,
            copy_commands=copy_commands,
            hardening=Image._HARDENING_BLOCK,
            clear_env=self.clear_env,
        )


@Instance.register("zeromicro", "go-zero")
class GoZero(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GoZeroImageDefault(self.pr, self._config)

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
                    base_name = get_base_name(test_name)
                    if base_name in failed_tests:
                        continue
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    passed_tests.add(base_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        passed_tests.remove(base_name)
                    if base_name in skipped_tests:
                        skipped_tests.remove(base_name)
                    failed_tests.add(base_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    base_name = get_base_name(test_name)
                    if base_name in passed_tests:
                        continue
                    if base_name not in failed_tests:
                        continue
                    skipped_tests.add(base_name)

        # Safety net: if a test appears in both passed and failed (e.g. same
        # test name in different packages), prefer the failure signal.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
