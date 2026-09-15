from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PR_LOW = 142
PR_HIGH = 142
INTERVAL = f"{PR_LOW}_to_{PR_HIGH}"
INTERVAL_NAME = f"three_bvh_csg_{INTERVAL}"

NODE_IMAGE = "node:16-bullseye"

JSON_BEGIN = "<<<JEST_JSON_BEGIN>>>"
JSON_END = "<<<JEST_JSON_END>>>"
REPORT_PATH = "/tmp/jest-report.json"

TEST_CMD = (
    f"rm -f {REPORT_PATH}\n"
    "cd /home/[[REPO]]/tests\n"
    "jest_status=0\n"
    "npx --no-install jest --ci --colors=false "
    f"--json --outputFile={REPORT_PATH} || jest_status=$?\n"
    'echo "JEST_EXIT_STATUS=$jest_status"\n'
    f"test -f {REPORT_PATH}\n"
    f'echo "{JSON_BEGIN}"\n'
    f"cat {REPORT_PATH}\n"
    'echo ""\n'
    f'echo "{JSON_END}"'
)

PRUNE = r"""RUN set -eux; \
    cd /home/[[REPO]]; \
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \
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
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/[[REPO]]/.gitmodules ]; then \
        cd /home/[[REPO]] && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi"""

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export HUSKY=0

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund --loglevel=error \
    || npm install --no-audit --no-fund --loglevel=error \
    || true

node --version
npm --version
test -x node_modules/.bin/jest
test -d tests
node -e "require.resolve('three'); require.resolve('three-mesh-bvh'); require('./package.json'); console.log('DEPS_OK')"
"""

STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export NODE_ENV=test

cd /home/[[REPO]]

[[PATCH_STEP]]
[[TEST_CMD]]
"""

TEST_PATCH_STEP = "git apply --whitespace=nowarn /home/test.patch\n"

FIX_PATCH_STEP = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"


class THREE_BVH_CSG_142_TO_142_ImageBase(Image):
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
        return f"base-{INTERVAL}"

    def workdir(self) -> str:
        return f"base-{INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
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

ENV npm_config_audit=false \\
    npm_config_fund=false \\
    npm_config_update_notifier=false \\
    HUSKY=0 \\
    HUSKY_SKIP_INSTALL=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class THREE_BVH_CSG_142_TO_142_ImageDefault(Image):
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
        return THREE_BVH_CSG_142_TO_142_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_CMD]]", self._test_cmd())
        )

    def _test_cmd(self) -> str:
        return TEST_CMD.replace("[[REPO]]", self.pr.repo)

    def _stage(self, patch_step: str) -> str:
        return self._expand(STAGE_SH).replace("[[PATCH_STEP]]", patch_step)

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", self._stage("")),
            File(".", "test-run.sh", self._stage(TEST_PATCH_STEP)),
            File(".", "fix-run.sh", self._stage(FIX_PATCH_STEP)),
        ]

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {name}:{tag}

{self.global_env}

{copies(self.install_files())}
{copies(self.grading_files())}
RUN bash /home/prepare.sh

{self._expand(PRUNE)}

{self.clear_env}
"""


@Instance.register("gkjohnson", INTERVAL_NAME)
class THREE_BVH_CSG_142_TO_142(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return THREE_BVH_CSG_142_TO_142_ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def _relative_suite_path(self, suite_name: str) -> str:
        path = (suite_name or "").replace("\\", "/")
        prefix = f"/home/{self.pr.repo}/"
        if path.startswith(prefix):
            return path[len(prefix) :]
        marker = "/tests/"
        idx = path.find(marker)
        if idx >= 0:
            return path[idx + 1 :]
        return path.rsplit("/", 1)[-1]

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        outcome = {
            "passed": passed,
            "failed": failed,
            "pending": skipped,
            "todo": skipped,
            "disabled": skipped,
            "skipped": skipped,
        }

        start = log.find(JSON_BEGIN)
        end = log.find(JSON_END, start + 1) if start >= 0 else -1
        blob = log[start + len(JSON_BEGIN) : end].strip() if end > start >= 0 else ""

        report = {}
        if blob:
            try:
                report = json.loads(blob)
            except json.JSONDecodeError:
                report = {}

        for suite in report.get("testResults", []) or []:
            rel = self._relative_suite_path(suite.get("name") or "")
            assertions = suite.get("assertionResults") or []
            if not assertions:
                if suite.get("status") == "failed":
                    failed.add(rel)
                continue
            for case in assertions:
                bucket = outcome.get(case.get("status", ""))
                if bucket is None:
                    continue
                title = case.get("fullName") or case.get("title") or ""
                bucket.add(f"{rel}::{title}".strip())

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


Instance.register("gkjohnson", "three-bvh-csg")(THREE_BVH_CSG_142_TO_142)
Instance.register("gkjohnson", INTERVAL)(THREE_BVH_CSG_142_TO_142)
