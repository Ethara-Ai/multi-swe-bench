import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _sanitize_patch(patch: str) -> str:
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    generated = re.compile(r"(^|/)(dist|build|\.output)/")

    sections: list[tuple[str, list[str]]] = []
    preamble: list[str] = []
    current: Optional[tuple[str, list[str]]] = None

    for line in patch.splitlines(keepends=True):
        match = header.match(line.rstrip("\n"))
        if match:
            current = (match.group(1), [line])
            sections.append(current)
        elif current is not None:
            current[1].append(line)
        else:
            preamble.append(line)

    kept: list[str] = list(preamble)
    for path, lines in sections:
        body = "".join(lines)
        is_binary = "GIT binary patch" in body or "Binary files " in body
        has_index = bool(re.search(r"^index [0-9a-f]+\.\.[0-9a-f]+", body, re.M))
        if is_binary and not has_index:
            continue
        if is_binary and "Binary files " in body:
            continue
        if generated.search(path):
            continue
        kept.append(body)

    return "".join(kept)


class SiyuanImageBase(Image):

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
        return "golang:1.25-bookworm"

    def image_tag(self) -> str:
        return "base-go1.25"

    def workdir(self) -> str:
        return "base-go1.25"

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

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN go version

{self.clear_env}

CMD ["/bin/bash"]
"""


class SiyuanImageDefault(Image):

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return SiyuanImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(".", "fix.patch", f"{filtered_fix_patch}"),
            File(".", "test.patch", f"{filtered_test_patch}"),
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
set -euo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git cat-file -e {pr.base.sha} 2>/dev/null \\
    || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" || true
git checkout --detach {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

cd /home/{pr.repo}/kernel
export CGO_ENABLED=1
export GOFLAGS=-mod=mod

go mod download || true
go build ./search/...

echo DEPS_OK

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export CGO_ENABLED=1
export GOFLAGS=-mod=mod

cd /home/{pr.repo}/kernel
go test -v -count=1 ./search/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export CGO_ENABLED=1
export GOFLAGS=-mod=mod

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{pr.repo}/kernel
go test -v -count=1 ./search/...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export CGO_ENABLED=1
export GOFLAGS=-mod=mod

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd /home/{pr.repo}/kernel
go test -v -count=1 ./search/...

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        if self.config.need_clone:
            clone = (
                f'RUN git clone "https://github.com/{self.pr.org}/{self.pr.repo}.git" '
                f"/home/{self.pr.repo}"
            )
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{clone}

WORKDIR /home/{self.pr.repo}

RUN git config --global --add safe.directory /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
CMD ["/bin/bash"]
"""


@Instance.register("siyuan-note", "siyuan_17846_to_17846")
class SIYUAN_17846_TO_17846(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SiyuanImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^--- PASS:\s+(\S+)")
        re_fail = re.compile(r"^--- FAIL:\s+(\S+)")
        re_skip = re.compile(r"^--- SKIP:\s+(\S+)")

        def base_name(name: str) -> str:
            index = name.rfind("/")
            return name if index == -1 else name[:index]

        for raw_line in clean_log.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            match = re_pass.match(line)
            if match:
                passed_tests.add(base_name(match.group(1)))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(base_name(match.group(1)))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(base_name(match.group(1)))

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


Instance.register("siyuan-note", "siyuan")(SIYUAN_17846_TO_17846)
