import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image, _safe_path_component
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO122 = "golang:1.22"

_PKG_EXCLUDE = (
    "cmd|internal|internalimport|generated|handler|middleware|registry|"
    "openapi|apis|version|gitutil|server|elasticsearch"
)

_GO_TEST_CMD = "go test -json -count=1 -gcflags=all=-l -timeout 10m $PKGS 2>&1"

_SELECT_PKGS = f"""PKGS="$(go list -e ./pkg/... | grep -vE '{_PKG_EXCLUDE}' || true)"
if [ -z "$PKGS" ]; then
    echo "karpor: package selection produced no packages"
    exit 1
fi"""


_NO_PRUNE_HARDENING = chr(10).join(
    line
    for line in Image._HARDENING_BLOCK.split(chr(10))
    if "git gc --prune=now" not in line and "git repack -a -d -l" not in line
)


class KarporImageBase(Image):

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
        return _GO122

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = _safe_path_component(self.pr.repo)
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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

{self.global_env}

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git fetch --no-tags origin "+refs/pull/*/head:refs/remotes/origin/pr/*"

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_NO_PRUNE_HARDENING}

{self.clear_env}

CMD ["/bin/bash"]
"""


class KarporImageDefault(Image):
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
        return KarporImageBase(self.pr, self.config)

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


if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "check_git_changes: not inside a git work tree: $(pwd)" >&2
    exit 1
fi

changes="$(git status --porcelain)"
if [ -n "$changes" ]; then
    echo "check_git_changes: working tree is not clean" >&2
    echo "$changes" >&2
    exit 1
fi
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

go mod download || true
go build ./... || true
go test -count=1 -gcflags=all=-l -run XXX_NO_SUCH_TEST ./pkg/... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{select}
{test_cmd}

""".format(pr=self.pr, select=_SELECT_PKGS, test_cmd=_GO_TEST_CMD),
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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("KusionStack", "karpor")
class Karpor(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return KarporImageDefault(self.pr, self._config)

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

    _MODULE_PREFIX = re.compile(r"^github\.com/KusionStack/(?:karbour|karpor)/")

    @classmethod
    def _qualify(cls, pkg: str, test: str) -> str:
        short = cls._MODULE_PREFIX.sub("", pkg)
        return f"{short}::{test}"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        build_failed_re = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        pkg_actions: dict[str, str] = {}
        pkg_has_tests: set[str] = set()

        for raw in clean_log.split("\n"):
            line = raw.strip()

            if not line.startswith("{"):
                match = build_failed_re.match(line)
                if match:
                    failed_tests.add(self._qualify(match.group(1), "[build]"))
                continue

            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            pkg = event.get("Package")
            test = event.get("Test")

            if action == "output" and pkg and not test:
                match = build_failed_re.match(str(event.get("Output", "")).strip())
                if match:
                    failed_tests.add(self._qualify(match.group(1), "[build]"))
                continue

            if not pkg or action not in ("pass", "fail", "skip"):
                continue

            if not test:
                pkg_actions[pkg] = action
                continue

            pkg_has_tests.add(pkg)
            name = self._qualify(pkg, test)
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        for pkg, action in pkg_actions.items():
            if action == "fail" and pkg not in pkg_has_tests:
                failed_tests.add(self._qualify(pkg, "[build]"))

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
