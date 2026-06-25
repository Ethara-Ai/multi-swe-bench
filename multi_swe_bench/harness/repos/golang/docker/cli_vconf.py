"""docker/cli harness for the pre-modules vendor.conf era.

Routes by number_interval == "cli_vconf" in the JSONL. Covers PRs whose
base.sha has vendor.conf and no vendor.mod (pre-Go-modules layout). Uses
golang:1.13: Go 1.14 introduced a new testing.T.Cleanup(func()) signature
that's incompatible with the gotest.tools/x/subtest vendored in this era,
so we pin to the last pre-1.14 release. Runs in GOPATH mode with the
source linked at /go/src/github.com/docker/cli.

Base = single shared reference-format image (# syntax opt-out, ARG REPO_URL,
ethara.ai LABEL, TZ=UTC, light hardening). PR layer appends the canonical
_HARDENING_BLOCK (literal base.sha) inside the real GOPATH git tree so
the fix cannot be read from git history (anti reward-hacking).
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



class CliVconfImageBase(Image):
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
        return "golang:1.13"

    def image_tag(self) -> str:
        return "base-vconf-2_to_4032"

    def workdir(self) -> str:
        return "base-vconf-2_to_4032"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # GOPATH layout: the repo lives at $GOPATH/src/github.com/docker/cli.
        # /home/cli is a symlink to that location so prepare/run scripts can
        # use either path interchangeably.
        if self.config.need_clone:
            code = (
                "RUN mkdir -p /go/src/github.com/docker && "
                f'git clone "${{REPO_URL}}" /go/src/github.com/{self.pr.org}/{self.pr.repo} && '
                f"ln -s /go/src/github.com/{self.pr.org}/{self.pr.repo} /home/{self.pr.repo}"
            )
        else:
            code = (
                f"RUN mkdir -p /go/src/github.com/{self.pr.org}\n"
                f"COPY {self.pr.repo} /go/src/github.com/{self.pr.org}/{self.pr.repo}\n"
                f"RUN ln -s /go/src/github.com/{self.pr.org}/{self.pr.repo} /home/{self.pr.repo}"
            )

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    GO111MODULE=off \\
    GOPATH=/go \\
    CGO_ENABLED=0 \\
    TZ=UTC
ENV PATH=/go/bin:/usr/local/go/bin:$PATH

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

# debian:buster moved to archive.debian.org; fix sources before apt-get update.
RUN if grep -q buster /etc/apt/sources.list 2>/dev/null; then \\
      sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; /buster-updates/d' /etc/apt/sources.list; \\
    fi && \\
    apt-get update && apt-get install -y --no-install-recommends git ca-certificates && \\
    rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /go/src/github.com/{self.pr.org}/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


class CliVconfImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return CliVconfImageBase(self.pr, self.config)

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

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /go/src/github.com/{pr.org}/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/') || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /go/src/github.com/{pr.org}/{pr.repo}
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /go/src/github.com/{pr.org}/{pr.repo}
git apply --whitespace=nowarn --exclude='*.binpb' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.mp4' --exclude='*.webp' /home/test.patch
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /go/src/github.com/{pr.org}/{pr.repo}
git apply --whitespace=nowarn --exclude='*.binpb' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' --exclude='*.svg' --exclude='*.ico' --exclude='*.pdf' --exclude='*.mp4' --exclude='*.webp' /home/test.patch /home/fix.patch
go test -v -count=1 $(go list ./... | grep -vE '/vendor/|/e2e/')

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        # Per-PR FULL hardening (prepare.sh has checked out base.sha): strip refs +
        # gc-prune so future/fix commits are unreachable & deleted, then audit.
        # Runs in the real GOPATH git tree (/home/{repo} is a symlink to it).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /go/src/github.com/{self.pr.org}/{self.pr.repo}

{hardening}
{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("docker", "cli_vconf")
class CLI_VCONF(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CliVconfImageDefault(self.pr, self._config)

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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

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
                    passed_tests.add(test_name)

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(test_name)

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name in failed_tests:
                        continue
                    skipped_tests.add(test_name)

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

# === bundle number_interval routing (prs_in_bundle dash-joined) ===
_BUNDLE_NIS_CLI_VCONF = [
    '2-3-3236',
    '2532-2538-2558-2560',
    '2724-2725-2749-2750-2758-2760-2776-2781-2797-2810',
    '2883-2886-2896',
    '3000-3002-3007-3036-3042',
    '3514-3516-3521-3522-3523-3526-3527-3530-3532-3551-3556-3563',
    '4032-4055-4060-4066-4091-4125',
    '2124-2150-2176-2177-2178',
    '2184-2195-2222-2239-2240-2261-2264-2265-2266-2267-2268-2276-2291-2292-2302-2311-2315-2320',
    '2364-2391-2393-2399-2401-2403-2419-2431-2435-2442-2444-2445-2453-2456-2457-2469-2470-2471-2481-2484-2493-2508-2509-2516-2518-2520',
    '2548-2557-2570-2575-2586-2589-2591-2592',
    '2957-2958-2959-2960-2961-2962-2963-2964',
    '3069-3071-3078-3079-3093-3099-3100-3107-3111',
    '3123-3132-3156-3174-3175-3176-3196-3200-3205-3219-3224',
    '3525-3926-3927-3928-3941-3944-3950-3951-3953-3957-3959-3967-3976-3979',
    '3707-3726-3743-3746-3747-3752-3753-3754-3773',
]
for _ni in _BUNDLE_NIS_CLI_VCONF:
    Instance.register("docker", _ni)(CLI_VCONF)
