import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

INTERVAL_NAME = "storybook_26298"
NODE_IMAGE = "node:18-bookworm"

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM {base_image}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""

GIT_PIN_AND_HARDENING = """RUN git reset --hard
RUN git checkout ${BASE_COMMIT}

RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

BUILD_ENV = """export CI=true
export PUPPETEER_SKIP_DOWNLOAD=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export CYPRESS_INSTALL_BINARY=0
export NO_COLOR=1
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
export NODE_OPTIONS=--max_old_space_size=4096
export NX_NO_CLOUD=true
"""

COMPILE = """corepack enable || true
yarn task --task compile --start-from=auto --no-link --debug || true
"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

{build_env}
{compile}
"""

RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

{build_env}
cd /home/{repo}/code
npx vitest run --reporter=verbose {test_files} 2>&1
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{build_env}
{compile}
cd /home/{repo}/code
npx vitest run --reporter=verbose {test_files} 2>&1
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{build_env}
{compile}
cd /home/{repo}/code
npx vitest run --reporter=verbose {test_files} 2>&1
"""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
DURATION_RE = re.compile(r"\s+\d+(?:\.\d+)?\s*m?s$")
FILE_LINE_RE = re.compile(
    r"^\s*[✓✔✕✗×↓○◌]\s+(\S+\.(?:[cm]?[jt]sx?))\s+\(\d+\s+tests?[^)]*\)"
)
PASSED_RE = re.compile(r"^\s*[✓✔]\s+(.+)$")
FAILED_RE = re.compile(r"^\s*[✕✗×]\s+(.+)$")
SKIPPED_RE = re.compile(r"^\s*[↓○◌]\s+(.+)$")


def parse_vitest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    log = ANSI_RE.sub("", test_log)

    current_file = None

    for line in log.splitlines():
        file_line = FILE_LINE_RE.match(line)
        if file_line:
            current_file = file_line.group(1)
            continue

        for pattern, bucket in (
            (PASSED_RE, passed_tests),
            (FAILED_RE, failed_tests),
            (SKIPPED_RE, skipped_tests),
        ):
            matched = pattern.match(line)
            if not matched:
                continue
            name = DURATION_RE.sub("", matched.group(1)).strip()
            if not name:
                break
            if current_file and not name.startswith(current_file):
                name = "{file} > {name}".format(file=current_file, name=name)
            bucket.add(name)
            break

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


def collect_test_files(test_patch: str) -> list[str]:
    files = []
    for path in re.findall(r"^diff --git a/\S+ b/(\S+)", test_patch or "", re.M):
        if ".test." in path or ".spec." in path or "__tests__/" in path:
            if path.startswith("code/"):
                path = path[len("code/") :]
            files.append(path)
    return sorted(set(files))


class ImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return "base-{name}".format(name=INTERVAL_NAME)

    def workdir(self) -> str:
        return "base-{name}".format(name=INTERVAL_NAME)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return BASE_DOCKERFILE.format(
            base_image=NODE_IMAGE,
            org=self.pr.org,
            repo=self.pr.repo,
        )


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        test_files = " ".join(collect_test_files(self.pr.test_patch))

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                PREPARE_SH.format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    build_env=BUILD_ENV,
                    compile=COMPILE,
                ),
            ),
            File(
                ".",
                "run.sh",
                RUN_SH.format(
                    repo=self.pr.repo,
                    build_env=BUILD_ENV,
                    test_files=test_files,
                ),
            ),
            File(
                ".",
                "test-run.sh",
                TEST_RUN_SH.format(
                    repo=self.pr.repo,
                    build_env=BUILD_ENV,
                    compile=COMPILE,
                    test_files=test_files,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                FIX_RUN_SH.format(
                    repo=self.pr.repo,
                    build_env=BUILD_ENV,
                    compile=COMPILE,
                    test_files=test_files,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += "COPY {name} /home/\n".format(name=file.name)

        sections = ["FROM {name}:{tag}".format(name=name, tag=tag)]

        if self.global_env:
            sections.append(self.global_env)

        sections.append('ARG BASE_COMMIT="{sha}"'.format(sha=self.pr.base.sha))
        sections.append(copy_commands.strip())
        sections.append("RUN bash /home/prepare.sh")
        sections.append(GIT_PIN_AND_HARDENING.strip())

        if self.clear_env:
            sections.append(self.clear_env)

        return "\n\n".join(sections) + "\n"


@Instance.register("storybookjs", INTERVAL_NAME)
class STORYBOOK_26298(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        return parse_vitest_log(test_log)
