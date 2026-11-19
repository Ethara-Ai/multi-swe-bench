import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# wealthfolio/wealthfolio, PR 2542 (base commit 2026-11-07).
#
# Graded tests: the frontend vitest files the test.patch touches
# (apps/frontend/src/**/*.test.ts[x]), run the way CI's "Frontend Tests" job does
# (`pnpm --filter frontend exec vitest run`, .github/workflows/pr-check.yml).
# The Rust half of the test.patch is not graded: the four Rust test modules
# live in crates/core, whose sources the fix rewrites (71 inline #[test]s ride
# in the fix patch itself, so they cannot be credited), and compiling the
# workspace in every stage would not fit the time budget under emulation.
#
# Toolchain pins, read at the base sha:
#   * Node.js 24.22.1 -- .node-version (CI: setup-node node-version-file).
#   * pnpm 10.33.4 -- package.json "packageManager" (CI: pnpm/action-setup).
#   * install -- `pnpm install --frozen-lockfile`, honouring pnpm-lock.yaml.

_INTERVAL = "wealthfolio_2542_to_2542"
# Official Docker library image, via its ECR Public mirror (same multi-arch manifest).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:24.22.1-bookworm"
_PNPM_VERSION = "10.33.4"
_APT = "git ca-certificates build-essential python3 curl"
_JS_ROOT = "apps/frontend"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_TEST_FILE = re.compile(r"^apps/frontend/src/.+\.(test|spec)\.[cm]?[jt]sx?$")
_RESULT = re.compile(r"^MSB_RESULT (passed|failed|skipped|pending|todo|disabled) (.+)$")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
    # Base commit not reachable from any cloned ref: fetch exactly that commit.
    git fetch --no-tags origin [[SHA]]
fi
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION (build time, pinned by pnpm-lock.yaml) ----------
# Same install as CI's Frontend Tests job.
pnpm install --frozen-lockfile

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# vitest and the jsdom environment the frontend config selects must resolve
# from the frontend package.
cd /home/[[REPO]]/[[JSROOT]]
node --version
pnpm --version
pnpm exec vitest --version
node -e "require.resolve('jsdom'); require.resolve('vitest/package.json'); console.log('deps ok')"
echo DEPS_OK
"""

_REPORT_JS = """'use strict';
// Prints one stable line per test case from a jest/vitest JSON report.
const fs = require('fs');
const path = require('path');
const [report, root] = process.argv.slice(2);
if (!fs.existsSync(report)) {
  console.log('MSB_NO_REPORT');
  process.exit(0);
}
const data = JSON.parse(fs.readFileSync(report, 'utf8'));
const clean = (s) => String(s).replace(/\\s+/g, ' ').trim();
for (const file of data.testResults || []) {
  const rel = path.relative(root, file.name || file.testFilePath || '');
  for (const a of file.assertionResults || []) {
    const parts = [...(a.ancestorTitles || []), a.title].map(clean);
    console.log(`MSB_RESULT ${a.status} ${rel}::${parts.join(' > ')}`);
  }
}
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
mapfile -t files < /home/test_files.txt
rm -f /tmp/msb-vitest.json
cd [[JSROOT]]
rc=0
pnpm exec vitest run --testTimeout=60000 --hookTimeout=60000 --reporter=default --reporter=json --outputFile.json=/tmp/msb-vitest.json "${files[@]}" || rc=$?
node /home/report_json.cjs /tmp/msb-vitest.json /home/[[REPO]]
exit $rc
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in _FILE_HEADER.finditer(patch)))


def _test_files(pr: PullRequest) -> str:
    keep = [f for f in _patch_files(pr.test_patch) if _TEST_FILE.match(f)]
    return "".join(f"{f[len(_JS_ROOT) + 1:]}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[JSROOT]]", _JS_ROOT)
        .replace("[[REPO]]", pr.repo)
    )


class Wealthfolio2542To2542ImageBase(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {_APT} \\
    && rm -rf /var/lib/apt/lists/*

# pnpm {_PNPM_VERSION} (package.json packageManager).
RUN npm install -g pnpm@{_PNPM_VERSION} && test "$(pnpm --version)" = "{_PNPM_VERSION}"

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    git config --global init.defaultBranch main

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Wealthfolio2542To2542ImageDefault(Image):
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
        return Wealthfolio2542To2542ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)),
            File(".", "test_files.txt", _test_files(pr)),
            File(".", "report_json.cjs", _REPORT_JS),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("wealthfolio", _INTERVAL)
class WEALTHFOLIO_2542_TO_2542(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Wealthfolio2542To2542ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        for raw in clean.splitlines():
            result = _RESULT.match(raw.strip())
            if not result:
                continue
            status, name = result.group(1), result.group(2)
            if status == "passed":
                passed_tests.add(name)
            elif status == "failed":
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
