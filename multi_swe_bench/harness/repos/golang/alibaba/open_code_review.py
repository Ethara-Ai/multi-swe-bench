import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenCodeReviewImageBase(Image):
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
        return "golang:1.26.10-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL=https://github.com/{self.pr.org}/{self.pr.repo}.git
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt

ENV SSL_CERT_FILE=${{CA_CERT_PATH}}
ENV REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CURL_CA_BUNDLE=${{CA_CERT_PATH}}

RUN mkdir -p /etc/pki/tls/certs && ln -sf "${{CA_CERT_PATH}}" /etc/pki/tls/certs/ca-bundle.crt

{self.global_env}

ENV CI=true
ENV GOTOOLCHAIN=auto
ENV CGO_ENABLED=1
ENV LC_ALL=C

RUN apt-get update \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates curl build-essential git gnupg make python3 sudo wget \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenCodeReviewImageDefault(Image):
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
        return OpenCodeReviewImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def test_command(self) -> str:
        return "go test -json -race -count=1 ./..."

    def git_apply(self, *patches: str) -> str:
        binary_paths = sorted(
            {
                path[2:]
                for patch in (self.pr.test_patch, self.pr.fix_patch)
                for pair in re.findall(r"^Binary files (\S+) and (\S+) differ$", patch, re.M)
                for path in pair
                if path.startswith(("a/", "b/"))
            }
        )
        excludes = "".join(f" --exclude='{path}'" for path in binary_paths)
        return f"git apply --whitespace=nowarn{excludes} " + " ".join(patches)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

go mod download
go test -race -count=1 -run '^$' ./... > /dev/null
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_command}
""".format(pr=self.pr, test_command=self.test_command()),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{apply}
{test_command}
""".format(pr=self.pr, apply=self.git_apply("/home/test.patch"), test_command=self.test_command()),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{apply}
{test_command}
""".format(pr=self.pr, apply=self.git_apply("/home/test.patch", "/home/fix.patch"), test_command=self.test_command()),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(
            f"COPY {file.name} /home/{file.name}\n" for file in self.files()
        )

        return f"""FROM {image.image_name()}:{image.image_tag()}

{self.global_env}

ARG TARGETARCH
ARG REPO_URL=https://github.com/{self.pr.org}/{self.pr.repo}.git
ARG BASE_COMMIT={self.pr.base.sha}
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt

ENV SSL_CERT_FILE=${{CA_CERT_PATH}}
ENV REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CURL_CA_BUNDLE=${{CA_CERT_PATH}}
ENV CI=true

RUN mkdir -p /etc/pki/tls/certs && ln -sf "${{CA_CERT_PATH}}" /etc/pki/tls/certs/ca-bundle.crt

{copy_commands}
RUN bash /home/prepare.sh

RUN cd /home/{self.pr.repo} \\
    && git remote | xargs -r -n1 git remote remove \\
    && git for-each-ref --format='%(refname)' | xargs -r -n1 git update-ref --no-deref -d \\
    && git reflog expire --expire=now --all \\
    && git reflog expire --expire-unreachable=now --all \\
    && git gc --prune=now \\
    && test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}" \\
    && test -z "$(git for-each-ref)" \\
    && test -z "$(git remote)" \\
    && test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("alibaba", "open-code-review")
class OpenCodeReview(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpenCodeReviewImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            package = event.get("Package")
            test = event.get("Test")
            action = event.get("Action")
            if not package or not test:
                continue

            name = f"{package}::{test}"
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            elif action == "skip":
                skipped_tests.add(name)

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
