import json
import os
import re
import shlex

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = os.path.basename(os.path.dirname(os.path.abspath(__file__))).replace("_", "-")
_ERA = os.path.splitext(os.path.basename(__file__))[0]
_GO_IMAGE = "golang:1.26.0-bookworm"
_INSTALL_ATTEMPTS = 3
_TEST_FILE = re.compile(r"_test\.go$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_GO_TEST = "go test -json -count=1 -timeout 30m ./..."

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


def _sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part) + "\n"


def _test_files(pr: PullRequest) -> list[str]:
    paths = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({path for path in paths if _TEST_FILE.search(path)})


def _go_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            "export GOTOOLCHAIN=local",
            "export GOFLAGS=-buildvcs=false",
            "export CGO_ENABLED=1",
        )
    )


def _module_dirs_block(pr: PullRequest) -> str:
    quoted = " ".join(shlex.quote(path) for path in _test_files(pr))
    return f"""module_dirs=()
for f in {quoted}; do
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
        d=$(dirname "$d")
    done
    if [ -f "$d/go.mod" ] && [[ ! " ${{module_dirs[*]}} " =~ " $d " ]]; then
        module_dirs+=("$d")
    fi
done
test "${{#module_dirs[@]}}" -gt 0"""


def _retry_block() -> str:
    return f"""retry() {{
    for attempt in $(seq 1 {_INSTALL_ATTEMPTS}); do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -eq {_INSTALL_ATTEMPTS} ]; then
            return 1
        fi
        sleep 15
    done
}}"""


def _prepare_script(pr: PullRequest) -> str:
    repo = pr.repo
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _go_env(),
        f"""cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        "go version",
        _retry_block(),
        _module_dirs_block(pr),
        f"""for d in "${{module_dirs[@]}}"; do
    echo "prepare: go module $d"
    (cd "$d" && retry go mod download)
    (cd "$d" && go test -count=1 -run '^$' ./... > /dev/null)
    packages=$(cd "$d" && go list ./... | wc -l)
    echo "prepare: $packages packages in $d"
    test "$packages" -gt 0
done""",
    )


def _run_script(pr: PullRequest, apply: str = "") -> str:
    setup = "\n".join(line for line in (f"cd /home/{pr.repo}", apply) if line)
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _go_env(),
        setup,
        _module_dirs_block(pr),
        f"""rc=0
for d in "${{module_dirs[@]}}"; do
    (cd "$d" && {_GO_TEST}) || rc=$?
done
exit "$rc\"""",
    )


class CockpitTools1120To1120ImageBase(Image):
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
        hi, lo = re.search(r"_(\d+)_to_(\d+)$", _ERA).groups()
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOTOOLCHAIN=local \\
    GOPROXY=https://proxy.golang.org,direct

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN git config --system --add safe.directory '*' && go version && git --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class CockpitTools1120To1120ImageDefault(Image):
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
        return CockpitTools1120To1120ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_apply = "git apply --whitespace=nowarn /home/test.patch"
        fix_apply = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script(self.pr)),
            File(".", "test-run.sh", _run_script(self.pr, test_apply)),
            File(".", "fix-run.sh", _run_script(self.pr, fix_apply)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {file.name} /home/" for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            copies,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _ERA)
class COCKPIT_TOOLS_1120_TO_1120(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return CockpitTools1120To1120ImageDefault(self.pr, self._config)

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
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}
        failed_packages: set[str] = set()
        packages_with_failed_tests: set[str] = set()
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            package = event.get("Package") or event.get("ImportPath") or ""
            test = event.get("Test")
            if action not in ("pass", "fail", "skip", "build-fail") or not package:
                continue
            if test:
                buckets[action].add(f"{package}::{test}")
                if action == "fail":
                    packages_with_failed_tests.add(package)
            elif action in ("fail", "build-fail"):
                failed_packages.add(package.split(" ")[0])
        for package in failed_packages - packages_with_failed_tests:
            failed_tests.add(f"{package}::<package failed>")
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
