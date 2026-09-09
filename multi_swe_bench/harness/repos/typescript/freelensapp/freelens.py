import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NL = "\n"


class FreelensImageBase(Image):
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
        return "node:22-bookworm"

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

        global_env = f"{_NL}{self.global_env}{_NL}" if self.global_env else ""
        clear_env = f"{_NL}{self.clear_env}{_NL}" if self.clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
ENV DO_NOT_TRACK=1
ENV NODE_OPTIONS=--max-old-space-size=8192
{global_env}
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates && rm -rf /var/lib/apt/lists/*

RUN npm install -g corepack@0.36.0 && corepack enable

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
{clear_env}"""


class FreelensImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return FreelensImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
set -eo pipefail

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh

test "$(git rev-parse HEAD)" = "{base_sha}"
bash /home/check_git_changes.sh

corepack enable
corepack pnpm config set enablePrePostScripts false

node --version
corepack pnpm --version

corepack pnpm install --prefer-offline --frozen-lockfile

NODE_ENV=production corepack pnpm --stream -r --filter './packages/**' build
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "resync.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

if ! corepack pnpm install --prefer-offline --frozen-lockfile; then
    echo "resync: lockfile changed by the patch -- re-running unfrozen" >&2
    corepack pnpm install --prefer-offline
fi

NODE_ENV=production corepack pnpm --stream -r --filter './packages/**' build
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}

git apply --summary /home/test.patch | sed -n 's/^ delete mode [0-9]* //p' > /tmp/retired_suites.txt
if [ -s /tmp/retired_suites.txt ]; then
    echo "run: excluding suites retired by the test patch:" >&2
    cat /tmp/retired_suites.txt >&2
    xargs -r rm -f < /tmp/retired_suites.txt
fi

corepack pnpm --stream -r --no-bail test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

if ! bash /home/resync.sh; then
    echo "resync: rebuild failed under the test patch (expected pre-fix); running the suites anyway" >&2
fi

corepack pnpm --stream -r --no-bail test:unit --verbose
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

bash /home/resync.sh

corepack pnpm --stream -r --no-bail test:unit --verbose
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("FreelensImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        global_env = f"{_NL}{self.global_env}{_NL}" if self.global_env else ""
        clear_env = f"{_NL}{self.clear_env}{_NL}" if self.clear_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
{global_env}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}{clear_env}"""


@Instance.register("freelensapp", "freelens")
class Freelens(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FreelensImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_stream_prefix = re.compile(r"^\s*(\S+)\s+test:unit:\s?")

        def qualify(pkg: str, name: str) -> str:
            return f"{pkg} > {name}" if pkg else name

        re_pass_file = re.compile(r"^PASS\s+(\S+)")
        re_fail_file = re.compile(r"^FAIL\s+(\S+)")

        timing = r"(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?"
        re_pass_test = re.compile(rf"^\s+[✓✔]\s+(.+?){timing}$")
        re_fail_test = re.compile(rf"^\s+[✕✗×]\s+(.+?){timing}$")
        re_skip_test = re.compile(
            rf"^\s+[○◌]\s+(?:skipped\s+)?(.+?){timing}$"
        )
        re_todo_test = re.compile(rf"^\s+✎\s+todo\s+(.+?){timing}$")

        for raw_line in clean_log.splitlines():
            stripped = raw_line.rstrip()
            prefix = re_stream_prefix.match(stripped)
            pkg = prefix.group(1) if prefix else ""
            line = stripped[prefix.end() :] if prefix else stripped

            m = re_fail_file.match(line)
            if m:
                failed_tests.add(qualify(pkg, m.group(1)))
                continue

            m = re_pass_file.match(line)
            if m:
                passed_tests.add(qualify(pkg, m.group(1)))
                continue

            m = re_fail_test.match(line)
            if m:
                failed_tests.add(qualify(pkg, m.group(1).strip()))
                continue

            m = re_pass_test.match(line)
            if m:
                passed_tests.add(qualify(pkg, m.group(1).strip()))
                continue

            m = re_skip_test.match(line)
            if m:
                skipped_tests.add(qualify(pkg, m.group(1).strip()))
                continue

            m = re_todo_test.match(line)
            if m:
                skipped_tests.add(qualify(pkg, m.group(1).strip()))

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
