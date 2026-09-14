import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_IMAGE = "golang:1.21-bookworm"
_BASE_APT = (
    "git ca-certificates build-essential pkg-config xvfb xauth"
    " libxcursor-dev libxrandr-dev libxinerama-dev libxi-dev"
    " libgl1-mesa-dev libsdl2-dev libasound2-dev"
)
_TEST_CMD = "xvfb-run --auto-servernum go test -json -count=1 ./..."

_PREPARE_TEMPLATE = """set -e
STATUS_LOG=/home/__REPO__/prepare-status.log
: > "$STATUS_LOG"
cd /home/__REPO__
echo "PREPARE_HEAD=$(git rev-parse HEAD)" >> "$STATUS_LOG"
echo "PREPARE_TREE_DIRTY_LINES=$(git status --porcelain | wc -l)" >> "$STATUS_LOG"
echo "PREPARE_GO_VERSION=$(go version)" >> "$STATUS_LOG"

if grep -qi qemu /proc/self/maps 2>/dev/null; then
  STRICT=0
else
  STRICT=1
fi
echo "PREPARE_STRICT=$STRICT" >> "$STATUS_LOG"

set +e
go mod download all
GOMOD_EXIT=$?
set -e
echo "PREPARE_GOMOD_EXIT=$GOMOD_EXIT" >> "$STATUS_LOG"
test "$GOMOD_EXIT" = "0"

set +e
go build ./... 2>&1 | tail -n 40
BUILD_EXIT=${PIPESTATUS[0]}
set -e
echo "PREPARE_BUILD_EXIT=$BUILD_EXIT" >> "$STATUS_LOG"

if [ "$STRICT" = "1" ]; then
  test "$BUILD_EXIT" = "0"
fi
echo "DEPS_OK"
"""

_CHECK_GIT_CHANGES = """set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi
echo "check_git_changes: No uncommitted changes"
exit 0
"""

_RUN_TEMPLATE = """set -eo pipefail
cd /home/__REPO__
git reset --hard
git clean -qfd
export CI=true
export GOFLAGS=-mod=mod
export CGO_ENABLED=1
__APPLY__set +e
__TEST_CMD__
echo "TEST_EXIT_CODE=$?"
"""


def _render(template: str, repo: str) -> str:
    return template.replace("__REPO__", repo).replace("__TEST_CMD__", _TEST_CMD)


def _run_script(repo: str, apply_cmd: str) -> str:
    return _render(_RUN_TEMPLATE, repo).replace("__APPLY__", apply_cmd)


class OpenDiablo2ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    CGO_ENABLED=1 \\
    GOFLAGS=-mod=mod \\
    GOTOOLCHAIN=local \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends {_BASE_APT} && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class OpenDiablo2ImageDefault(Image):
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
        return OpenDiablo2ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE_TEMPLATE, repo)),
            File(".", "run.sh", _run_script(repo, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(repo, "git apply --whitespace=nowarn /home/test.patch\n"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).rstrip("\n")
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {name}:{tag}\n\n"
            f"WORKDIR /home/{repo}\n\n"
            "RUN git reset --hard\n\n"
            f"RUN git checkout {sha}\n\n"
            f"{hardening}\n\n"
            "WORKDIR /home/\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n"
        )


@Instance.register("OpenDiablo2", "OpenDiablo2")
class OPEN_DIABLO2(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenDiablo2ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def _rel_pkg(self, package: str) -> str:
        module = f"github.com/{self.pr.org}/{self.pr.repo}"
        if package == module:
            return ""
        prefix = module + "/"
        if package.startswith(prefix):
            return package[len(prefix) :]
        return package

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        for line in clean_log.splitlines():
            line = line.strip()
            if not line.startswith("{") or '"Action"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue

            name = event.get("Test")
            if not name:
                continue
            action = event.get("Action")
            if action not in ("pass", "fail", "skip"):
                continue

            pkg = self._rel_pkg(event.get("Package", ""))
            test_id = f"{pkg}::{name}" if pkg else name

            if action == "pass":
                passed_tests.add(test_id)
            elif action == "fail":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
