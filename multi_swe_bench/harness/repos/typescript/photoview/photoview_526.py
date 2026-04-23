import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_GO_TEST_CMD = "CGO_ENABLED=1 go test -v -count=1 ./..."


def _clean_test_name(name: str) -> str:
    name = re.sub(
        r"\s+\(\d+\s+tests?(?:\s*\|\s*\d+\s+\w+)*\)\s*(?:\d+(?:\.\d+)?\s*m?s)?\s*$",
        "",
        name,
    )
    name = re.sub(r"\s+\(\d+(?:\.\d+)?\s*m?s\)\s*$", "", name)
    return name.strip()


def _get_go_base_name(test_name: str) -> str:
    index = test_name.rfind("/")
    if index == -1:
        return test_name
    return test_name[:index]


class PhotoviewImageBase_526(Image):
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
        return "golang:1.16-bullseye"

    def image_tag(self) -> str:
        return "base-node18"

    def workdir(self) -> str:
        return "base-node18"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git"
                f" /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV CGO_ENABLED=1

# Install Node.js 18 via nodesource
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \\
    && mkdir -p /etc/apt/keyrings \\
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \\
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_18.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

# Install Go system deps for CGO (go-face, go-sqlite3, libheif)
RUN apt-get update && apt-get install -y --no-install-recommends \\
    pkg-config gcc libc6-dev \\
    libheif-dev libdlib-dev libblas-dev liblapack-dev \\
    libjpeg62-turbo-dev libatlas-base-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class PhotoviewImageDefault_526(Image):
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
        return PhotoviewImageBase_526(self.pr, self._config)

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Install frontend deps
cd /home/{pr.repo}/ui
npm install --legacy-peer-deps || true
npm install graphql --legacy-peer-deps || true

# Warm-up frontend tests (failures tolerated)
CI=true npx craco test --setupFilesAfterEnv ./testing/setupTests.ts --verbose --ci --watchAll=false || true

# Download Go deps
cd /home/{pr.repo}/api
go mod download || true

# Warm-up Go tests (failures tolerated)
{go_test_cmd} || true

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}

# Run frontend tests
cd /home/{pr.repo}/ui
npm install --legacy-peer-deps || true
npm install graphql --legacy-peer-deps || true
CI=true npx craco test --setupFilesAfterEnv ./testing/setupTests.ts --verbose --ci --watchAll=false || true

# Run Go tests
cd /home/{pr.repo}/api
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- .
git apply --whitespace=nowarn /home/test.patch

# Run frontend tests
cd /home/{pr.repo}/ui
npm install --legacy-peer-deps || true
npm install graphql --legacy-peer-deps || true
CI=true npx craco test --setupFilesAfterEnv ./testing/setupTests.ts --verbose --ci --watchAll=false || true

# Run Go tests
cd /home/{pr.repo}/api
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git checkout -- .
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

# Run frontend tests
cd /home/{pr.repo}/ui
npm install --legacy-peer-deps || true
npm install graphql --legacy-peer-deps || true
CI=true npx craco test --setupFilesAfterEnv ./testing/setupTests.ts --verbose --ci --watchAll=false || true

# Run Go tests
cd /home/{pr.repo}/api
{go_test_cmd}

""".format(pr=self.pr, go_test_cmd=_GO_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                    RUN go env -w GOPROXY=https://proxy.golang.org,direct
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )

        return f"""FROM {name}:{tag}

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

"""


@Instance.register("photoview", "photoview_526")
class Photoview_526(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PhotoviewImageDefault_526(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.splitlines():
            stripped = line.strip()
            if not stripped:
                continue

            # Go test patterns
            m = re.match(r"--- PASS: (\S+)", stripped)
            if m:
                test_name = m.group(1)
                base_name = _get_go_base_name(test_name)
                if base_name not in failed_tests:
                    if base_name in skipped_tests:
                        skipped_tests.discard(base_name)
                    passed_tests.add(base_name)
                continue

            m = re.match(r"--- FAIL: (\S+)", stripped)
            if m:
                test_name = m.group(1)
                base_name = _get_go_base_name(test_name)
                passed_tests.discard(base_name)
                skipped_tests.discard(base_name)
                failed_tests.add(base_name)
                continue

            m = re.match(r"--- SKIP: (\S+)", stripped)
            if m:
                test_name = m.group(1)
                base_name = _get_go_base_name(test_name)
                if base_name not in passed_tests and base_name not in failed_tests:
                    skipped_tests.add(base_name)
                continue

            # Vitest/Jest file-level PASS
            m = re.match(r"PASS\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                passed_tests.add(name)
                continue

            # Vitest/Jest file-level FAIL
            m = re.match(r"FAIL\s+(.+?)$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                failed_tests.add(name)
                continue

            # Vitest/Jest test-level pass
            m = re.match(r"[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                passed_tests.add(name)
                continue

            # Vitest/Jest test-level fail
            m = re.match(r"[×✕✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                failed_tests.add(name)
                continue

            # Vitest/Jest skipped
            m = re.match(r"[↓○]\s+(.+?)(?:\s+\[skipped\])?$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                skipped_tests.add(name)
                continue

            # Jest skipped (○ skipped pattern)
            m = re.match(r"○\s+skipped\s+(.+)$", stripped)
            if m:
                name = _clean_test_name(m.group(1).strip())
                skipped_tests.add(name)
                continue

        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
