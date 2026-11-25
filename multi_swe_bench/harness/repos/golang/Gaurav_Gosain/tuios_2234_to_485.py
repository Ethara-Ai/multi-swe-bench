import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "Gaurav-Gosain"
_REPO = "tuios"
_INTERVAL = "tuios_2234_to_485"
_BASE_TAG = "base-2234_to_485"
_GO_IMAGE = "golang:1.27.4-bookworm"

_ENV_EXPORTS = """export CI=true
export GOTOOLCHAIN=local
export GOFLAGS=-buildvcs=false
export CGO_ENABLED=1
export USER="${USER:-root}"
"""

_BINARY_EXCLUDES = (
    "$(awk '/^diff --git /{p=$NF} /^Binary files /{print \"--exclude=\" substr(p,3)}' "
    "[[PATCHES]])"
)

_REGEN_ARCHIVES = r"""for p in $(awk '/^diff --git /{p=$NF} /^Binary files /{print substr(p,3)}' [[PATCHES]]); do
    d=$(dirname "$p")
    if [ -f "$d/gen.go" ]; then
        (cd "$d" && go run gen.go)
    fi
done"""

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

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

[[ENV]]
go mod download || true
(cd e2e/tui && go mod download) || true

go version
go build ./...
go test -count=1 -run '^$' -exec /bin/true ./... > /dev/null
(cd e2e/tui && go test -count=1 -run '^$' -exec /bin/true ./... > /dev/null)
echo DEPS_OK
"""

_RUN_SH = r"""#!/bin/bash
set -eo pipefail

[[ENV]]
export TUIOS_E2E=1

cd /home/[[REPO]]
[[APPLY]]

modroot() {
    local d="$1"
    while [ "$d" != "." ] && [ ! -f "$d/go.mod" ]; do
        d=$(dirname "$d")
    done
    echo "$d"
}

touched() {
    awk -v d="$1" '/^diff --git a\// { f = $4; sub(/^b\//, "", f); r = substr(f, length(d) + 2); keep = (substr(f, 1, length(d) + 1) == d "/" && r !~ /\// && r ~ /_test\.go$/); if (keep) print "FILE " f; next } keep && /^\+func Test[A-Za-z0-9_]*\([A-Za-z_][A-Za-z0-9_]* \*testing\.T\)/ { s = $0; sub(/^\+func /, "", s); sub(/\(.*/, "", s); print "TEST " s }' /home/test.patch
}

e2e_names() {
    local kind name
    touched "$1" | while read -r kind name; do
        if [ "$kind" = TEST ]; then
            echo "$name"
        elif git cat-file -e "HEAD:$name" 2> /dev/null; then
            git show "HEAD:$name" | sed -nE 's/^func (Test[A-Za-z0-9_]*)\([A-Za-z_][A-Za-z0-9_]* \*testing\.T\).*/\1/p'
        fi
    done
}

dirs=$(awk '/^diff --git a\// { f = $4; sub(/^b\//, "", f); if (f ~ /_test\.go$/) { d = f; if (sub(/\/[^\/]*$/, "", d) == 0) d = "."; print d } }' /home/test.patch | sort -u)
test -n "$dirs"

rc=0
for d in $dirs; do
    [ -d "$d" ] || continue
    mod=$(modroot "$d")
    if [ "$mod" = "$d" ]; then
        pkg=.
    elif [ "$mod" = . ]; then
        pkg="./$d"
    else
        pkg="./${d#"$mod"/}"
    fi
    sel=()
    if [ "$d" = e2e/tui ]; then
        names=$(e2e_names "$d" | sort -u | paste -sd '|' -)
        [ -n "$names" ] || continue
        sel=(-run "^($names)\$")
    fi
    (cd "$mod" && go test -json -count=1 -timeout 60m "${sel[@]}" "$pkg" 2>&1) \
        | awk '!/"Action":"(output|run|pause|cont|start)"/ || /--- FAIL|panic:/' || rc=1
done
exit "$rc"
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
    GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false \\
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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates \\
        curl \\
        git \\
        build-essential \\
        procps \\
        lsof \\
        tmux \\
        python3 \\
        openssh-client \\
    && rm -rf /var/lib/apt/lists/*

RUN test -s /etc/ssl/certs/ca-certificates.crt && \\
    git --version && \\
    gcc --version > /dev/null && \\
    go version

RUN git config --global --add safe.directory '*'

__CODE__

CMD ["/bin/bash"]
"""


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[ENV]]", _ENV_EXPORTS)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
    )


def _apply(patches: str) -> str:
    excludes = _BINARY_EXCLUDES.replace("[[PATCHES]]", patches)
    regen = _REGEN_ARCHIVES.replace("[[PATCHES]]", patches)
    return f"git apply --whitespace=nowarn {excludes} {patches}\n{regen}"


class Tuios2234To485ImageBase(Image):
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
        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return (
            _BASE_DOCKERFILE.replace("__IMAGE__", self.dependency())
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", repo)
            .replace("__CODE__", code)
        )


class Tuios2234To485ImageDefault(Image):
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
        return Tuios2234To485ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(_RUN_SH, self.pr, _apply("/home/test.patch")),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(_RUN_SH, self.pr, _apply("/home/test.patch /home/fix.patch")),
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

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}
{self.clear_env}
"""


@Instance.register(_ORG, _INTERVAL)
class TUIOS_2234_TO_485(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Tuios2234To485ImageDefault(self.pr, self._config)

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


Instance.register(_ORG, _REPO)(TUIOS_2234_TO_485)
