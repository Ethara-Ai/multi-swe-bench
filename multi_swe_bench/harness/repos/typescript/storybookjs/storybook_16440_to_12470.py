import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

INTERVAL_NAME = "storybook_16440_to_12470"
NODE_IMAGE = "node:14-bullseye"

JEST_ERA_MAX_PR = 16440
VITEST_ERA_MIN_PR = 26298
VITEST_ERA_KEY = "storybook_26298"

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

RUN set -eux; \\
    for i in 1 2 3; do \\
        apt-get update && \\
        apt-get install -y --no-install-recommends --fix-missing \\
            ca-certificates curl build-essential git gnupg make python3 sudo wget \\
        && break || {{ echo "apt attempt $i failed, retrying"; sleep 10; }}; \\
    done; \\
    rm -rf /var/lib/apt/lists/*

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
export ADBLOCK=1
export NO_COLOR=1
export NODE_OPTIONS=--max_old_space_size=4096
"""

INSTALL_AND_BUILD = """export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_NODE_LINKER=node-modules
YARN_MAJOR="$(yarn --version 2>/dev/null | cut -d. -f1)"
echo "detected yarn $(yarn --version 2>/dev/null) (major=${YARN_MAJOR})"
if [ "${YARN_MAJOR}" = "1" ]; then
  yarn install --network-timeout 600000 || true
else
  yarn install || true
fi
yarn bootstrap --build || true
if [ ! -x node_modules/.bin/jest ]; then
  echo "jest binary missing after install; retrying"
  if [ "${YARN_MAJOR}" = "1" ]; then
    yarn install --network-timeout 600000 --check-files || true
  else
    yarn install --no-immutable || true
  fi
fi
test -x node_modules/.bin/jest || { echo "FATAL: jest unavailable after yarn install"; exit 1; }
"""

PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

{build_env}
{install_and_build}
"""

RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

{build_env}
npx --no-install jest --ci --runInBand --verbose --passWithNoTests {test_files} 2>&1
"""

TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

{build_env}
{install_and_build}
npx --no-install jest --ci --runInBand --verbose --passWithNoTests {test_files} 2>&1
"""

FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{build_env}
{install_and_build}
npx --no-install jest --ci --runInBand --verbose --passWithNoTests {test_files} 2>&1
"""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
TIMING_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")
SUITE_RE = re.compile(r"^(PASS|FAIL)\s+(\S+)")
PASSED_RE = re.compile(r"^\s+[✓✔]\s+(.+)$")
FAILED_RE = re.compile(r"^\s+[✕✗×]\s+(.+)$")
SKIPPED_RE = re.compile(r"^\s+[○◌◯✎]\s+(.+)$")
SKIP_PREFIX_RE = re.compile(r"^(?:skipped|todo)\s+", re.IGNORECASE)


def parse_jest_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    log = ANSI_RE.sub("", test_log)

    current_suite = None
    suite_status: dict[str, str] = {}
    suite_case_count: dict[str, int] = {}

    for line in log.splitlines():
        suite = SUITE_RE.match(line)
        if suite:
            current_suite = suite.group(2)
            suite_status[current_suite] = suite.group(1)
            suite_case_count.setdefault(current_suite, 0)
            continue

        for pattern, bucket, strip_prefix in (
            (PASSED_RE, passed_tests, False),
            (FAILED_RE, failed_tests, False),
            (SKIPPED_RE, skipped_tests, True),
        ):
            matched = pattern.match(line)
            if not matched:
                continue
            name = TIMING_RE.sub("", matched.group(1)).strip()
            if strip_prefix:
                name = SKIP_PREFIX_RE.sub("", name).strip()
            if not name:
                break
            if current_suite:
                name = "{suite} > {name}".format(suite=current_suite, name=name)
                suite_case_count[current_suite] += 1
            bucket.add(name)
            break

    for suite, status in suite_status.items():
        if suite_case_count.get(suite, 0) == 0 and status == "FAIL":
            failed_tests.add(suite)

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
                    install_and_build=INSTALL_AND_BUILD,
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
                    install_and_build=INSTALL_AND_BUILD,
                    test_files=test_files,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                FIX_RUN_SH.format(
                    repo=self.pr.repo,
                    build_env=BUILD_ENV,
                    install_and_build=INSTALL_AND_BUILD,
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


@Instance.register("storybookjs", "storybook")
@Instance.register("storybookjs", INTERVAL_NAME)
class STORYBOOK_16440_TO_12470(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if pr.number <= JEST_ERA_MAX_PR:
            return super().__new__(cls)
        if pr.number >= VITEST_ERA_MIN_PR:
            key = "{org}/{name}".format(org=pr.org, name=VITEST_ERA_KEY)
            return Instance._registry[key](pr, config, *args, **kwargs)
        raise ValueError(
            "PR #{number} falls between the verified eras of "
            "{org}/{repo}: <=#{jest_max} is the yarn-1/jest layout "
            "({jest_key}), >=#{vitest_min} is the code/ yarn-4/vitest "
            "layout ({vitest_key}). The toolchain in between is "
            "unverified; add an era config covering it.".format(
                number=pr.number,
                org=pr.org,
                repo=pr.repo,
                jest_max=JEST_ERA_MAX_PR,
                jest_key=INTERVAL_NAME,
                vitest_min=VITEST_ERA_MIN_PR,
                vitest_key=VITEST_ERA_KEY,
            )
        )

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
        return parse_jest_log(test_log)
