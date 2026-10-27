import json as _json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_REPO_PREFIX = "github.com/anchore/grype/"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def parse_go_test_log(log: str) -> TestResult:
    log = _ANSI_RE.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for raw in log.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = _json.loads(raw)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue

        test = event.get("Test")
        action = event.get("Action")
        package = event.get("Package") or ""
        if not test or action not in ("pass", "fail", "skip"):
            continue
        if package.startswith(_REPO_PREFIX):
            package = package[len(_REPO_PREFIX):]

        name = f"{package}::{test}"
        if action == "pass":
            passed_tests.add(name)
        elif action == "fail":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

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


class GrypeEraImageBase(Image):
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
        return "golang:1.26-bookworm"

    def image_tag(self) -> str:
        return "base-3787_to_3546"

    def workdir(self) -> str:
        return "base-3787_to_3546"

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
    GOFLAGS="-buildvcs=false -mod=mod" \\
    GOPATH=/go \\
    GOMODCACHE=/go/pkg/mod \\
    GRYPE_DB_AUTO_UPDATE=false \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \\
 && ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN go version && go env GOPATH

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class GrypeEraImageDefault(Image):
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
        return GrypeEraImageBase(self.pr, self._config)

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
set -eo pipefail

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
set -eo pipefail

cd /home/{pr.repo}

git config --global --add safe.directory /home/{pr.repo}
git config --global user.email "test@example.com"
git config --global user.name "grype-test"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

timeout 2700 go mod download || true

test "$(git rev-parse HEAD)" = "{pr.base.sha}"
go build ./...
go list -deps -test ./... > /dev/null
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export GOFLAGS="-buildvcs=false -mod=mod"
export GOTOOLCHAIN=local
export GRYPE_DB_AUTO_UPDATE=false

cd /home/{pr.repo}

timeout 2700 go mod download

go test -json -count=1 -timeout 90m ./... 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export GOFLAGS="-buildvcs=false -mod=mod"
export GOTOOLCHAIN=local
export GRYPE_DB_AUTO_UPDATE=false

cd /home/{pr.repo}

git apply --whitespace=nowarn /home/test.patch

timeout 2700 go mod download

go test -json -count=1 -timeout 90m ./... 2>&1
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LC_ALL=C.UTF-8
export GOFLAGS="-buildvcs=false -mod=mod"
export GOTOOLCHAIN=local
export GRYPE_DB_AUTO_UPDATE=false

cd /home/{pr.repo}

git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn /home/fix.patch

timeout 2700 go mod download

go test -json -count=1 -timeout 90m ./... 2>&1
""".format(pr=self.pr),
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

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout --detach {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
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
"""


@Instance.register("anchore", "grype_3787_to_3546")
class GRYPE_3787_TO_3546(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrypeEraImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_go_test_log(log)


Instance.register("anchore", "grype")(GRYPE_3787_TO_3546)
Instance.register("anchore", "3546-3705-3787")(GRYPE_3787_TO_3546)
