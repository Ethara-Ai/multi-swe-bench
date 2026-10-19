from __future__ import annotations

import json
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TAG_SUFFIX = "1734_to_1303"
_GO_IMAGE = "golang:1.20-bullseye"
_MODULE_PREFIX = "github.com/openkruise/kruise/"
_MODULE_ROOT = _MODULE_PREFIX.rstrip("/")
_ENVTEST_DIR = "/usr/local/kubebuilder/bin"
_ENVTEST_URL = (
    "https://github.com/kubernetes-sigs/controller-tools/releases/download/"
    "envtest-v${ENVTEST_K8S_VERSION}/envtest-v${ENVTEST_K8S_VERSION}-linux-${ENVTEST_ARCH}.tar.gz"
)
_FAKE_PLUGIN_DIR = "pkg/daemon/criruntime/imageruntime/fake_plugin"
_GO_TEST_CMD = "go test -json -count=1 -timeout=60m ./pkg/... 2>&1"

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
go env GOPATH GOMODCACHE GOFLAGS CGO_ENABLED

if [ -d vendor ]; then
  test -f vendor/modules.txt || {{ echo "prepare.sh: vendor directory without modules.txt"; exit 1; }}
else
  go mod download || {{ echo "prepare.sh: go mod download failed at the base commit"; exit 1; }}
fi

ENVTEST_K8S_VERSION=$(sed -n 's/^ENVTEST_K8S_VERSION[[:space:]]*?\\{{0,1\\}}=[[:space:]]*\\([0-9][0-9.]*\\).*/\\1/p' Makefile | head -1)
test -n "${{ENVTEST_K8S_VERSION}}" || {{ echo "prepare.sh: ENVTEST_K8S_VERSION not found in Makefile"; exit 1; }}
ENVTEST_ARCH=$(go env GOARCH)
export ENVTEST_K8S_VERSION ENVTEST_ARCH

mkdir -p {_ENVTEST_DIR}
curl -fsSL "{_ENVTEST_URL}" -o /tmp/envtest.tar.gz || {{ echo "prepare.sh: envtest assets download failed for ${{ENVTEST_K8S_VERSION}} linux-${{ENVTEST_ARCH}}"; exit 1; }}
tar -xzf /tmp/envtest.tar.gz -C {_ENVTEST_DIR} --strip-components=2
rm -f /tmp/envtest.tar.gz
export KUBEBUILDER_ASSETS={_ENVTEST_DIR}

if [ -f {_FAKE_PLUGIN_DIR}/main.go ]; then
  go build -o {_FAKE_PLUGIN_DIR}/fake-credential-plugin {_FAKE_PLUGIN_DIR}/main.go || {{ echo "prepare.sh: fake-credential-plugin build failed"; exit 1; }}
  chmod +x {_FAKE_PLUGIN_DIR}/fake-credential-plugin
fi

go build . ./cmd/... ./apis/... ./pkg/... || {{ echo "prepare.sh: go build of manager, daemon, apis and pkg failed at the base commit"; exit 1; }}
go test -count=1 -run '^$' -exec /bin/true ./pkg/... || {{ echo "prepare.sh: a test package failed to compile at the base commit"; exit 1; }}
test -x {_ENVTEST_DIR}/kube-apiserver
test -x {_ENVTEST_DIR}/etcd
echo DEPS_OK
"""


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export CGO_ENABLED=1
export KUBEBUILDER_ASSETS={_ENVTEST_DIR}
{apply_line}{_GO_TEST_CMD}
"""


def _is_repo_package(name: str) -> bool:
    return name.startswith(_MODULE_PREFIX) or name == _MODULE_ROOT


def _go_package(name: str) -> str:
    if name.startswith(_MODULE_PREFIX):
        return name[len(_MODULE_PREFIX) :]
    return "."


class KruiseImageBase_KRUISE_1734_TO_1303(Image):
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
    curl --version | head -1 && \\
    tar --version | head -1 && \\
    gcc --version | head -1 && \\
    test -f /etc/ssl/certs/ca-certificates.crt

ENV CGO_ENABLED=1 \\
    KUBEBUILDER_ASSETS={_ENVTEST_DIR}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class KruiseImageDefault_KRUISE_1734_TO_1303(Image):
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
        return KruiseImageBase_KRUISE_1734_TO_1303(self.pr, self.config)

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
                "KruiseImageDefault_KRUISE_1734_TO_1303 dependency must be an Image"
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


@Instance.register("openkruise", "kruise")
@Instance.register("openkruise", "kruise_1734_to_1303")
class KRUISE_1734_TO_1303(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return KruiseImageDefault_KRUISE_1734_TO_1303(self.pr, self._config)

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
