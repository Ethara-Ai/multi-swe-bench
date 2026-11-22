import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "traefik"
_INTERVAL = "yaegi_344_to_149"
_BASE_TAG = "base-344_to_149"
_GO_IMAGE = "golang:1.12.17-buster"
_GOPATH = "/go"
_IMPORT_PATH = "github.com/containous/yaegi"
_GO111MODULE = "off"
_REPO_DIR = f"{_GOPATH}/src/{_IMPORT_PATH}"
_DEPS_CMD = "go mod download" if _GO111MODULE == "on" else "go get -d -t ./..."

_ENV_EXPORTS = f"""export CI=true
export GOPATH={_GOPATH}
export GO111MODULE={_GO111MODULE}
export CGO_ENABLED=1
export PATH="{_GOPATH}/bin:/usr/local/go/bin:$PATH"
"""

_TEST_CMD = r"""go test -json -count=1 -vet=off -timeout 60m ./... 2>&1 \
    | awk '!/"Action":"(output|run|pause|cont|start)"/ || /--- FAIL|panic:/'
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    GIT_SSL_CAINFO=${CA_CERT_PATH} \\
    GOPATH=__GOPATH__ \\
    GO111MODULE=__GO111MODULE__ \\
    CGO_ENABLED=1 \\
    CI=true

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN test -s /etc/ssl/certs/ca-certificates.crt && \\
    git --version && \\
    gcc --version > /dev/null && \\
    go version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

__CODE__

CMD ["/bin/bash"]
"""


def _prepare_script(sha: str) -> str:
    return f"""#!/bin/bash
set -e

cd {_REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{_ENV_EXPORTS}
{_DEPS_CMD} || true

go version
go_minor="$(go version | sed -E 's/^go version go1\\.([0-9]+).*$/\\1/')"
test -n "$go_minor"
if ls stdlib/go1_*.go > /dev/null 2>&1; then
    ls stdlib/go1_"$go_minor"_*.go > /dev/null
fi
go build ./...
go test -count=1 -vet=off -run '^$' ./... > /dev/null
echo DEPS_OK
"""


def _run_script(apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd {_REPO_DIR}", apply)))
    return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
{setup}

{_TEST_CMD}"""


class Yaegi344To149ImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = (
                f'RUN mkdir -p "$(dirname {_REPO_DIR})" && \\\n'
                f'    git clone "${{REPO_URL}}" {_REPO_DIR} && \\\n'
                f"    ln -s {_REPO_DIR} /home/{self.pr.repo} && \\\n"
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = (
                f'RUN mkdir -p "$(dirname {_REPO_DIR})"\n'
                f"COPY {self.pr.repo} {_REPO_DIR}\n"
                f"RUN ln -s {_REPO_DIR} /home/{self.pr.repo}"
            )

        return (
            _BASE_DOCKERFILE.replace("__IMAGE__", self.dependency())
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__GOPATH__", _GOPATH)
            .replace("__GO111MODULE__", _GO111MODULE)
            .replace("__CODE__", code)
        )


class Yaegi344To149ImageDefault(Image):
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
        return Yaegi344To149ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr.base.sha)),
            File(".", "run.sh", _run_script()),
            File(
                ".",
                "test-run.sh",
                _run_script("git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR {_REPO_DIR}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register(_ORG, _INTERVAL)
class YAEGI_344_TO_149(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Yaegi344To149ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {
            "pass": passed_tests,
            "fail": failed_tests,
            "skip": skipped_tests,
        }

        for line in log.splitlines():
            line = line.strip()
            if not line.startswith("{") or '"Action"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            test = event.get("Test")
            package = event.get("Package")
            if action in buckets and test and package:
                buckets[action].add(f"{package}::{test}")

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
