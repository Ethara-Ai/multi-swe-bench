import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.16"

BASE_TAG = "base-1000_to_0"

NUMBER_INTERVAL = "stash_1000_to_0"

MODULE_PATH = "github.com/stashapp/stash"

GO_BUILD_TAGS = "integration sqlite_stat4 sqlite_math_functions"

GO_TEST_CMD = (
    f'CGO_ENABLED=1 go test -tags "{GO_BUILD_TAGS}" -v -count=1 -timeout 30m ./...'
)

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git \
        gcc libc6-dev pkg-config \
        libvips-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/*
"""

UI_STUB = """if [ -d ui ]; then
  mkdir -p ui/v2.5/build ui/v2/build
  [ -f ui/v2.5/build/index.html ] || echo '<!doctype html>' > ui/v2.5/build/index.html
  [ -f ui/v2/build/index.html ] || echo '<!doctype html>' > ui/v2/build/index.html
fi
"""

VENDOR_MODE = """if [ -d vendor ]; then
  export GOFLAGS="-mod=vendor"
fi
"""

GENERATE_STEP = """if [ -d cmd/stash ]; then
  go generate ./cmd/stash > /dev/null 2>&1 || true
  if [ -d internal/api/loaders ]; then
    go generate ./internal/api/loaders > /dev/null 2>&1 || true
  fi
else
  go generate ./... > /dev/null 2>&1 || true
fi
"""

BASELINE_GEN = """if [ "$(uname -m)" = "x86_64" ]; then
  go test -tags "integration sqlite_stat4 sqlite_math_functions" -v -count=1 -timeout 30m ./... 2>&1 | awk '
    /^(ok|FAIL|\?)[ \t]+/ { pkg=$2; for (i=1;i<=n;i++) print pkg "\t" names[i]; n=0; next }
    /^[ \t]*--- (PASS|FAIL|SKIP): / { names[++n]=$3 }
  ' > /home/baseline_tests.txt || true
fi
"""

SALVAGE_INLINE = """if [ -f /home/stage.log ] && grep -q '\[build failed\]' /home/stage.log; then
  grep '\[build failed\]' /home/stage.log | awk '{ print $2 }' | sort -u | while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    awk -F'\t' -v p="$pkg" '$1 == p { print "--- FAIL: " $2 " (0.00s)" }' /home/baseline_tests.txt
    printf 'FAIL\t%s\t0.000s\n' "$pkg"
  done
fi
"""


class Stash1000To0ImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    TZ=UTC \\
    CGO_ENABLED=1 \\
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

WORKDIR /home/

{TOOLCHAIN_SETUP}
RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class Stash1000To0ImageDefault(Image):
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
        return Stash1000To0ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh

{ui_stub}
{vendor_mode}if [ ! -d vendor ]; then
  go mod download || true
fi

if [ -d cmd/stash ]; then
  go generate ./cmd/stash || true
  if [ -d internal/api/loaders ]; then
    go generate ./internal/api/loaders || true
  fi
else
  go generate ./... || true
fi

if ! ls pkg/models/generated_*.go > /dev/null 2>&1 \\
   && ! ls internal/api/generated_*.go > /dev/null 2>&1; then
  echo "prepare.sh: code generation produced no generated_*.go" >&2
  exit 1
fi

go build -tags "{tags}" ./... || true

{baseline_gen}
git checkout -- .
""".format(
                    pr=self.pr,
                    ui_stub=UI_STUB,
                    vendor_mode=VENDOR_MODE,
                    baseline_gen=BASELINE_GEN,
                    tags=GO_BUILD_TAGS,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{ui_stub}{vendor_mode}{generate_step}
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc
""".format(
                    pr=self.pr,
                    ui_stub=UI_STUB,
                    vendor_mode=VENDOR_MODE,
                    generate_step=GENERATE_STEP,
                    salvage=SALVAGE_INLINE,
                    test_cmd=GO_TEST_CMD,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{ui_stub}{vendor_mode}{generate_step}
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc
""".format(
                    pr=self.pr,
                    ui_stub=UI_STUB,
                    vendor_mode=VENDOR_MODE,
                    generate_step=GENERATE_STEP,
                    salvage=SALVAGE_INLINE,
                    test_cmd=GO_TEST_CMD,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{ui_stub}{vendor_mode}{generate_step}
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc
""".format(
                    pr=self.pr,
                    ui_stub=UI_STUB,
                    vendor_mode=VENDOR_MODE,
                    generate_step=GENERATE_STEP,
                    salvage=SALVAGE_INLINE,
                    test_cmd=GO_TEST_CMD,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout {sha}

RUN set -eux; \\
    git checkout --detach "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh
"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_RESULT = re.compile(r"^--- (PASS|FAIL|SKIP): (\S+)")
_RE_PKG_END = re.compile(r"^(?:ok|FAIL|\?)\s+(\S+)")


def parse_go_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    pending: list[tuple[str, str]] = []
    orphans: list[tuple[str, str]] = []

    def relative(package: str) -> str:
        if package == MODULE_PATH:
            return "."
        prefix = MODULE_PATH + "/"
        if package.startswith(prefix):
            return package[len(prefix) :]
        return package

    def record(status: str, name: str) -> None:
        if status == "FAIL":
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif status == "PASS":
            if name not in failed_tests:
                skipped_tests.discard(name)
                passed_tests.add(name)
        elif status == "SKIP":
            if name not in passed_tests and name not in failed_tests:
                skipped_tests.add(name)

    for raw in _RE_ANSI.sub("", test_log).splitlines():
        line = raw.strip()

        match = _RE_RESULT.match(line)
        if match:
            pending.append((match.group(1), match.group(2)))
            continue

        match = _RE_PKG_END.match(line)
        if match:
            package = match.group(1)
            for status, name in pending:
                record(status, f"{relative(package)}::{name}")
            pending = []

    orphans.extend(pending)
    for status, name in orphans:
        record(status, name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("stashapp", NUMBER_INTERVAL)
class Stash1000To0(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Stash1000To0ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_go_log(test_log)
