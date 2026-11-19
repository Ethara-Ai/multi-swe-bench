from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "numaproj"
_REPO = "numaflow"
_INTERVAL = "numaflow_3558_to_3558"
BASE_TAG = f"base-{_INTERVAL}"
GO_IMAGE = "golang:1.25-bookworm"
MODULE_PREFIX = "github.com/numaproj/numaflow"
REPO_DIR = "/home/numaflow"
GO_TEST_FLAGS = "-short -p 2 -v -count=1 -timeout 20m"
BUILD_TEST_NAME = "<build>"

RACE_PROBE_SH = r"""_RACE=""
if (cd "$(mktemp -d)" && printf 'package main\nfunc main() {}\n' > probe.go \
        && GOFLAGS= go run -race probe.go) >/dev/null 2>&1; then
    _RACE="-race"
fi"""

GO_ENV_SH = r"""export GOFLAGS="-buildvcs=false"
export GOTOOLCHAIN=local
export GO111MODULE=on
export CGO_ENABLED=1
export GOPATH=/go
export GOCACHE=/go/cache
export GORACE=halt_on_error=1"""

_DIFF_PATHS_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_GOMOD_ADD_RE = re.compile(r"^\+(?:require\s+|\t)([A-Za-z0-9.\-_~/]+\.[A-Za-z0-9.\-_~/]+)\s+(v[0-9][^\s/]*)", re.M)


def target_test_files(pr: PullRequest) -> list[str]:
    pairs = _DIFF_PATHS_RE.findall(pr.test_patch or "")
    paths = {p for pair in pairs for p in pair if p.endswith("_test.go")}
    return sorted(p for p in paths if not p.startswith("vendor/"))


def target_packages(pr: PullRequest) -> list[str]:
    owners = {path.rsplit("/", 1)[0] for path in target_test_files(pr) if "/" in path}
    return sorted(f"./{owner}" for owner in owners)


def patch_added_modules(pr: PullRequest) -> list[str]:
    found = set()
    for patch in (pr.test_patch or "", pr.fix_patch or ""):
        for chunk in re.split(r"(?=^diff --git )", patch, flags=re.M):
            if not re.match(r"^diff --git a/(\S*/)?go\.mod b/", chunk):
                continue
            for mod, ver in _GOMOD_ADD_RE.findall(chunk):
                found.add(f"{mod}@{ver}")
    return sorted(found)


class NumaflowImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

ENV GOFLAGS="-buildvcs=false" \\
    GOTOOLCHAIN=local \\
    GO111MODULE=on \\
    CGO_ENABLED=1 \\
    GOPATH=/go \\
    GOCACHE=/go/cache \\
    GORACE=halt_on_error=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ca-certificates \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && \\
    git config --global core.longpaths true

RUN git clone "${{REPO_URL}}" {REPO_DIR} && \\
    cd {REPO_DIR} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e
git rev-parse --is-inside-work-tree > /dev/null 2>&1 || { echo "check_git_changes: Not inside a git repository"; exit 1; }
test -z "$(git status --porcelain)" || { echo "check_git_changes: Uncommitted changes"; git status --porcelain | head -20; exit 1; }
echo "check_git_changes: No uncommitted changes"
"""

PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    git fetch origin "[[SHA]]"
fi
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

[[GO_ENV]]

[[RACE_PROBE]]

go mod download

_EXTRA_MODS="[[EXTRA_MODS]]"
if [ -n "${_EXTRA_MODS}" ]; then
    _tmp="$(mktemp -d)"
    (cd "${_tmp}" && go mod init msb.local/prefetch >/dev/null 2>&1 && go get ${_EXTRA_MODS})
    rm -rf "${_tmp}"
fi

go test $_RACE -run '^$' -count=1 [[TEST_PKGS]] >/dev/null 2>&1 || true

go version
go vet -n ./... >/dev/null 2>/tmp/go_vet_n.log || { tail -50 /tmp/go_vet_n.log >&2; exit 1; }
rm -f /tmp/go_vet_n.log

_gate_pkgs=""
for _pkg in [[TEST_PKGS]]; do
    if compgen -G "${_pkg}/*.go" >/dev/null; then
        _gate_pkgs="${_gate_pkgs} ${_pkg}"
    fi
done
echo "race detector: ${_RACE:-unavailable on this host}"
go build ${_gate_pkgs:-./pkg/...}
go test $_RACE -run '^$' -count=1 ${_gate_pkgs:-./pkg/apis/...}
echo DEPS_OK
"""

TEST_SH = r"""#!/bin/bash
set -eo pipefail
export CI=true
export TZ=UTC

cd [[REPO_DIR]]

[[GO_ENV]]

_PKGS="[[TEST_PKGS]]"
test -n "$_PKGS" || {
    echo "test: this PR's test patch contains no Go test file; nothing to grade"
    exit 0
}

[[RACE_PROBE]]

go test $_RACE [[GO_TEST_FLAGS]] $_PKGS 2>&1
"""


class NumaflowImageDefault(Image):
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
        return NumaflowImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[GO_ENV]]", GO_ENV_SH)
            .replace("[[RACE_PROBE]]", RACE_PROBE_SH)
            .replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_PKGS]]", " ".join(target_packages(self.pr)))
            .replace("[[EXTRA_MODS]]", " ".join(patch_added_modules(self.pr)))
            .replace("[[GO_TEST_FLAGS]]", GO_TEST_FLAGS)
        )

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
            "fix-run.sh": "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
        }
        staged = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + step + "\nbash /home/test.sh\n"
                ),
            )
            for name, step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "test.sh", self._expand(TEST_SH)),
        ] + staged

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        sha = self.pr.base.sha

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {image.image_name()}:{image.image_tag()}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

{copies(self.install_files())}
RUN bash /home/prepare.sh

{copies(self.grading_files())}
WORKDIR {REPO_DIR}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f {REPO_DIR}/.gitmodules ]; then \\
        cd {REPO_DIR} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _INTERVAL)
class NUMAFLOW_3558_TO_3558(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NumaflowImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        module = re.escape(MODULE_PREFIX)
        result_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
        package_re = re.compile(r"^(?:ok|FAIL|---)\s+(" + module + r"\S*)")
        build_fail_re = re.compile(r"^FAIL\s+(" + module + r"\S*)\s+\[(?:build|setup) failed\]")
        build_ok_re = re.compile(r"^(?:ok|FAIL)\s+(" + module + r"\S*)\s+(?:[0-9]|\(cached\))")
        no_tests_re = re.compile(r"^\?\s+(" + module + r"\S*)\s+\[no test files\]")
        timeout_re = re.compile(r"^panic: test timed out after ")
        running_re = re.compile(r"^\s+(Test\S+) \([0-9]")
        in_timeout = False

        default_pkg = (target_packages(self.pr) or ["."])[0].lstrip(".").lstrip("/")
        fallback = f"{MODULE_PREFIX}/{default_pkg}" if default_pkg else MODULE_PREFIX

        buckets: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        outcome = {"PASS": "PASSED", "FAIL": "FAILED", "SKIP": "SKIPPED"}
        pending: list[tuple[str, str]] = []

        def flush(package: str) -> None:
            for status, name in pending:
                buckets[outcome[status]].add(f"{package}::{name}")
            pending.clear()
        for line in log.splitlines():
            line = line.rstrip()
            m = result_re.match(line)
            if m:
                pending.append((m.group(1), m.group(2)))
                continue
            m = build_fail_re.match(line)
            if m:
                buckets["FAILED"].add(f"{m.group(1)}::{BUILD_TEST_NAME}")
                flush(m.group(1))
                continue
            m = build_ok_re.match(line)
            if m:
                buckets["PASSED"].add(f"{m.group(1)}::{BUILD_TEST_NAME}")
                flush(m.group(1))
                continue
            if timeout_re.match(line):
                in_timeout = True
                continue
            if in_timeout:
                m = running_re.match(line)
                if m:
                    pending.append(("FAIL", m.group(1)))
                    continue
                if not line.strip() or line.startswith("goroutine "):
                    in_timeout = False
            if no_tests_re.match(line):
                continue
            m = package_re.match(line)
            if m:
                flush(m.group(1))
        flush(fallback)

        passed = buckets["PASSED"]
        failed = buckets["FAILED"]
        skipped = buckets["SKIPPED"]
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
