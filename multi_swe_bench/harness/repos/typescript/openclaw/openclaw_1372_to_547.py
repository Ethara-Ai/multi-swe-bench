import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class OpenclawImageBase(Image):
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
        return "node:22"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
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
{global_block}
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    git \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable && corepack prepare pnpm@10.23.0 --activate

WORKDIR /home/

{code}

RUN git config --global pack.windowMemory 256m \\
    && git config --global pack.threads 1

WORKDIR /home/{repo}
{clear_block}
CMD ["/bin/bash"]
"""


class OpenclawImageDefault(Image):
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
        return OpenclawImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {pr.base.sha} 2>/dev/null \\
    || git fetch --no-tags origin "+refs/pull/{pr.number}/head:refs/remotes/origin/pr-{pr.number}" 2>/dev/null \\
    || true
git cat-file -e {pr.base.sha}^{{commit}}

git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

CI=true pnpm install --frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || CI=true pnpm install --frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || CI=true pnpm install --no-frozen-lockfile --ignore-scripts=false --config.engine-strict=false --config.enable-pre-post-scripts=true \\
    || true

if [ ! -x node_modules/.bin/vitest ]; then
    echo "prepare.sh: pnpm install did not produce node_modules/.bin/vitest"
    exit 1
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
pnpm exec vitest run --reporter=verbose --no-file-parallelism --retry=2 --testTimeout=120000 --hookTimeout=180000

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
pnpm exec vitest run --reporter=verbose --no-file-parallelism --retry=2 --testTimeout=120000 --hookTimeout=180000

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fd
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch
export CI=true
export NODE_OPTIONS=--max-old-space-size=4096
pnpm exec vitest run --reporter=verbose --no-file-parallelism --retry=2 --testTimeout=120000 --hookTimeout=180000

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

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        hardening = Image._HARDENING_BLOCK.replace(
            '"${BASE_COMMIT}"', self.pr.base.sha
        ).rstrip("\n")

        return f"""FROM {name}:{tag}
{global_block}
{copy_commands}
WORKDIR /home/{self.pr.repo}

{hardening}

RUN bash /home/prepare.sh
{clear_block}"""


@Instance.register("openclaw", "openclaw_1372_to_547")
class OPENCLAW_1372_TO_547(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenclawImageDefault(self.pr, self._config)

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

        clean_log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]|\x00", "", test_log)

        spec_re = r"\S+\.(?:test|spec)\.[cm]?[jt]sx?"
        trailing_re = r"(?:\s+\d+(?:\.\d+)?\s*(?:ms|s))?(?:\s*\(retry\s+x\d+\))?"
        case_re = re.compile(
            rf"^\s*(?P<marker>[✓✔√×✕✖✗✘↓○])\s+"
            rf"(?P<name>{spec_re}\s+>\s+.*?)"
            rf"{trailing_re}\s*$",
            re.MULTILINE,
        )
        fail_case_re = re.compile(
            rf"^\s*FAIL\s+(?P<name>{spec_re}\s+>\s+.+?)\s*$", re.MULTILINE
        )

        pass_markers = {"✓", "✔", "√"}
        fail_markers = {"×", "✕", "✖", "✗", "✘"}
        skip_markers = {"↓", "○"}

        for m in case_re.finditer(clean_log):
            marker = m.group("marker")
            name = m.group("name").strip()
            if marker in pass_markers:
                passed_tests.add(name)
            elif marker in fail_markers:
                failed_tests.add(name)
            elif marker in skip_markers:
                skipped_tests.add(name)

        for m in fail_case_re.finditer(clean_log):
            failed_tests.add(m.group("name").strip())

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


Instance.register("openclaw", "openclaw")(OPENCLAW_1372_TO_547)
