import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BASE_TAG = "base"


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g; s|security.debian.org/debian-security|archive.debian.org/debian-security|g; /-updates/d' /etc/apt/sources.list && \
    apt-get -o Acquire::Check-Valid-Until=false update && apt-get install -y --no-install-recommends \
        git ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV GO111MODULE=on \
    GOPATH=/go \
    CGO_ENABLED=1

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/__REPO__

WORKDIR /home/__REPO__

CMD ["/bin/bash"]
"""


_DEFAULT_DOCKERFILE = r"""FROM __BASE_IMAGE__

__COPY_COMMANDS__
RUN bash /home/prepare.sh

RUN set -eux; \
    cd /home/__REPO__; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \
        cd /home/__REPO__ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
"""


_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
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


_GO_TEST_TARGETS = r"""module_root() {
    local dir="$1"
    while [ "$dir" != "." ] && [ "$dir" != "/" ]; do
        if [ -f "$dir/go.mod" ]; then
            echo "$dir"
            return
        fi
        dir=$(dirname "$dir")
    done
    echo "."
}

go_test_targets() {
    awk '
        /^diff --git / { hdr = 1; next }
        /^@@/ { hdr = 0; next }
        hdr && /^(--- |\+\+\+ |rename from |rename to )/ {
            p = $0
            sub(/^(--- |\+\+\+ |rename from |rename to )/, "", p)
            sub(/\t.*$/, "", p)
            if (p ~ /^".*"$/) p = substr(p, 2, length(p) - 2)
            if ($0 ~ /^(--- |\+\+\+ )/) sub(/^[ab]\//, "", p)
            if (p != "/dev/null" && p ~ /\.go$/) print p
        }
    ' /home/test.patch /home/fix.patch \
        | while read -r f; do dirname "$f"; done \
        | sort -u \
        | while read -r dir; do
            case "/$dir/" in */testdata/*|*/vendor/*) continue ;; esac
            [ -d "$dir" ] || continue
            compgen -G "$dir/*.go" > /dev/null || continue
            mod=$(module_root "$dir")
            if [ "$mod" = "$dir" ]; then
                pkg="."
            elif [ "$mod" = "." ]; then
                pkg="./$dir"
            else
                pkg="./${dir#"$mod"/}"
            fi
            echo "$mod|$pkg"
        done
}

TARGETS=$(go_test_targets)
"""


_GO_TEST_RUN = r"""if [ -z "$TARGETS" ]; then
    echo "no Go packages found in test.patch/fix.patch" >&2
    exit 1
fi
MODULES=$(echo "$TARGETS" | cut -d'|' -f1 | sort -u)

status=0
for mod in $MODULES; do
    pkgs=$(echo "$TARGETS" | awk -F'|' -v m="$mod" '$1 == m { print $2 }')
    cd "/home/__REPO__/$mod"
    go list -e -f '{{.ImportPath}} {{.Dir}}' $pkgs | while read -r importpath dir; do
        if [ "$dir" = "/home/__REPO__" ]; then
            reldir="."
        else
            reldir="${dir#/home/__REPO__/}"
        fi
        echo "GOTESTPKG $importpath $reldir"
        for path in "$dir"/*_test.go; do
            if [ ! -f "$path" ]; then
                continue
            fi
            if [ "$reldir" = "." ]; then
                relfile="$(basename "$path")"
            else
                relfile="$reldir/$(basename "$path")"
            fi
            awk -v p="$importpath" -v r="$relfile" \
                'match($0, /^func (Test|Example|Fuzz|Benchmark)[A-Za-z0-9_]*/) { print "GOTESTFUNC " p " " r " " substr($0, 6, RLENGTH - 5) }' \
                "$path"
        done
    done
    go test -json -count=1 -timeout 20m $pkgs || status=$?
    cd /home/__REPO__
done
exit $status
"""


_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "__BASE_SHA__"
bash /home/check_git_changes.sh

export CI=true

__GO_TEST_TARGETS__
if [ -z "$TARGETS" ]; then
    TARGETS=".|./..."
fi
MODULES=$(echo "$TARGETS" | cut -d'|' -f1 | sort -u)

for mod in $MODULES; do
    attempt=1
    until (cd "/home/__REPO__/$mod" && go mod download); do
        if [ "$attempt" -ge 3 ]; then
            echo "go mod download failed in $mod after 3 attempts" >&2
            exit 1
        fi
        sleep "$((attempt * 15))"
        attempt=$((attempt + 1))
    done
done

for mod in $MODULES; do
    pkgs=$(echo "$TARGETS" | awk -F'|' -v m="$mod" '$1 == m { print $2 }')
    (cd "/home/__REPO__/$mod" && go test -count=1 -run '^$' $pkgs)
done
echo "DEPS_OK"
"""


_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

__GO_TEST_TARGETS__
__GO_TEST_RUN__"""


_TEST_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch

__GO_TEST_TARGETS__
__GO_TEST_RUN__"""


_FIX_RUN_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true

cd /home/__REPO__

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

__GO_TEST_TARGETS__
__GO_TEST_RUN__"""


class OpenTelemetryGoImageBase(Image):

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
        return "golang:1.15.15-buster"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class OpenTelemetryGoImageDefault(Image):

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
        return OpenTelemetryGoImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__GO_TEST_TARGETS__", _GO_TEST_TARGETS)
            .replace("__GO_TEST_RUN__", _GO_TEST_RUN)
            .replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return (
            self._render(_DEFAULT_DOCKERFILE)
            .replace("__BASE_IMAGE__", image.image_full_name())
            .replace("__COPY_COMMANDS__", copy_commands)
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def opentelemetry_go_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    package_dirs: dict[str, str] = {}
    func_files: dict[tuple[str, str], str] = {}
    for line in clean_log.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "GOTESTPKG":
            package_dirs[parts[1]] = parts[2]
        elif len(parts) == 4 and parts[0] == "GOTESTFUNC":
            func_files.setdefault((parts[1], parts[3]), parts[2])

    for line in clean_log.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        package = event.get("Package")
        test = event.get("Test")
        if not package or not test:
            continue

        top_level = test.split("/", 1)[0]
        location = func_files.get((package, top_level)) or package_dirs.get(
            package, package
        )
        name = f"{location}::{test}"

        action = event.get("Action")
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        elif action == "skip":
            skipped_tests.add(name)

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


@Instance.register("open-telemetry", "opentelemetry-go")
class OpenTelemetryGo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpenTelemetryGoImageDefault(self.pr, self._config)

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
        return opentelemetry_go_parse_log(test_log)
