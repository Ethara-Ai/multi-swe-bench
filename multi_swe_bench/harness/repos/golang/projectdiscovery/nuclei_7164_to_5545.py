from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TAG_SUFFIX = "7164_to_5545"
_BASE_IMAGE = "golang:1.25-bookworm"
_GO_ROOT = "/usr/local/go-nuclei"
_MODULE_PREFIX = "github.com/projectdiscovery/nuclei/v3/"
_TEST_CMD = "go test -tags headless_local -json -count=1 -timeout 60m ./... 2>&1"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PLAIN_PKG_RE = re.compile(r"^(ok|FAIL)\s+(\S+)(?:\s|$)")


def _go_version(pr: PullRequest) -> str:
    if pr.number >= 6000:
        return "1.25.7"
    return "1.21.13"


class NucleiImageBase_NUCLEI_7164_TO_5545(Image):
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
        return _BASE_IMAGE

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

RUN printf 'Acquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99retries

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl build-essential python3 chromium \\
    && rm -rf /var/lib/apt/lists/*

ENV GOTOOLCHAIN=local

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class NucleiImageDefault_NUCLEI_7164_TO_5545(Image):
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
        return NucleiImageBase_NUCLEI_7164_TO_5545(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        go_env = f"""export GOROOT={_GO_ROOT}
export PATH={_GO_ROOT}/bin:$PATH
export GOTOOLCHAIN=local
export CGO_ENABLED=1
export CI=true"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"
bash /home/check_git_changes.sh

want="{_go_version(self.pr)}"
arch="$(dpkg --print-architecture)"
curl -fsSL "https://go.dev/dl/go$want.linux-$arch.tar.gz" -o /tmp/go.tgz || {{ echo "prepare.sh: download of go$want failed"; exit 1; }}
mkdir -p {_GO_ROOT}
tar -C {_GO_ROOT} --strip-components=1 -xzf /tmp/go.tgz
rm -f /tmp/go.tgz

{go_env}
go version

go mod download || {{ echo "prepare.sh: go mod download failed"; exit 1; }}
go run ./cmd/nuclei -ut || {{ echo "prepare.sh: nuclei-templates install failed"; exit 1; }}

test "$(go env GOVERSION)" = "go$want" || {{ echo "prepare.sh: go$want is not the active toolchain"; exit 1; }}
test "$(go env CGO_ENABLED)" = "1" || {{ echo "prepare.sh: CGO_ENABLED is not 1"; exit 1; }}
chromium --headless --no-sandbox --disable-gpu --dump-dom about:blank 2>/dev/null | grep -q "<body>" || {{ echo "prepare.sh: chromium cannot render headless"; exit 1; }}
test -n "$(find /root/nuclei-templates -name '*.yaml' -print -quit)" || {{ echo "prepare.sh: nuclei-templates missing after install"; exit 1; }}
go build ./... || {{ echo "prepare.sh: go build failed at the base commit"; exit 1; }}
go test -tags headless_local -count=1 -run '^$' ./... || {{ echo "prepare.sh: a test package failed to compile at the base commit"; exit 1; }}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{go_env}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{go_env}
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{go_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "NucleiImageDefault_NUCLEI_7164_TO_5545 dependency must be an Image"
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


def _strip_module(package: str) -> str:
    if package.startswith(_MODULE_PREFIX):
        return package[len(_MODULE_PREFIX) :]
    if package == _MODULE_PREFIX.rstrip("/"):
        return "."
    return package


def _parse_nuclei_log(test_log: str) -> TestResult:
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()
    buckets = {"pass": passed, "fail": failed, "skip": skipped}
    plain = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip", "ok": "pass"}

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            if action not in buckets:
                continue
            package = _strip_module(event.get("Package") or "")
            if not package:
                continue
            test = event.get("Test")
            if test:
                buckets[action].add(f"{package}::{test}")
            elif action in ("pass", "fail"):
                buckets[action].add(package)
            continue

        match = _PLAIN_PKG_RE.match(line)
        if match and match.group(2).startswith(_MODULE_PREFIX.rstrip("/")):
            buckets[plain[match.group(1)]].add(_strip_module(match.group(2)))

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("projectdiscovery", "nuclei_7164_to_5545")
class NUCLEI_7164_TO_5545(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return NucleiImageDefault_NUCLEI_7164_TO_5545(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_nuclei_log(test_log)
