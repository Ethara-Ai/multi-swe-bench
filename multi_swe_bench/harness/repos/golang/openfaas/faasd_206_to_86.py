from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "206_to_86"
_GO_IMAGE = "golang:1.16-bullseye"
_MODULE_PREFIX = "github.com/openfaas/faasd/"
_MODULE_ROOT = _MODULE_PREFIX.rstrip("/")
_GO_TEST_CMD = "go test -json -count=1 -timeout=20m ./... 2>&1"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_GO_PKG_LINE_RE = re.compile(r"^(ok|FAIL|\?)\s+(\S+)(?:\s|$)")

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
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
"""


def _prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

go version
go env GOPATH GOCACHE GOFLAGS CGO_ENABLED

go build ./... || {{ echo "prepare.sh: go build failed at the base commit"; exit 1; }}
go test -count=1 -run '^$' ./... || {{ echo "prepare.sh: a test package failed to compile at the base commit"; exit 1; }}
"""


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export CGO_ENABLED=1
{apply_line}{_GO_TEST_CMD}
"""


def _is_repo_package(name: str) -> bool:
    return name.startswith(_MODULE_PREFIX) or name == _MODULE_ROOT


def _go_package(name: str) -> str:
    if name.startswith(_MODULE_PREFIX):
        return name[len(_MODULE_PREFIX) :]
    return "."


class FaasdImageBase_FAASD_206_TO_86(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _GO_IMAGE

    def image_tag(self) -> str:
        return f"base-{_TAG_SUFFIX}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN git --version && \\
    make --version | head -1 && \\
    gcc --version | head -1 && \\
    test -f /etc/ssl/certs/ca-certificates.crt

ENV GOFLAGS=-mod=vendor \\
    CGO_ENABLED=1

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class FaasdImageDefault_FAASD_206_TO_86(Image):
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
        return FaasdImageBase_FAASD_206_TO_86(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(self.pr)),
            File(".", "run.sh", _run_sh(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                _run_sh(self.pr, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_sh(
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "FaasdImageDefault_FAASD_206_TO_86 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("openfaas", "faasd")
@Instance.register("openfaas", "faasd_206_to_86")
class FAASD_206_TO_86(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return FaasdImageDefault_FAASD_206_TO_86(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"pass": passed, "fail": failed, "skip": skipped}
        markers = {"ok": passed, "FAIL": failed, "?": skipped}

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            if not line.startswith("{"):
                match = _GO_PKG_LINE_RE.match(line)
                if match and _is_repo_package(match.group(2)):
                    markers[match.group(1)].add(_go_package(match.group(2)))
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            package = event.get("Package") or ""
            if action not in buckets or not _is_repo_package(package):
                continue

            test = event.get("Test")
            name = _go_package(package)
            buckets[action].add(f"{name}::{test}" if test else name)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
