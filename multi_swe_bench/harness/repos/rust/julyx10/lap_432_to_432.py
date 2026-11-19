import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# julyx10/lap, PR 432 (base commit 2026-10-15).
#
# The repo is a Tauri app (Rust backend in src-tauri/, Vue frontend in
# src-vite/). Every test the PR adds is a frontend `node:test` file
# (src-vite/tests/*.test.mjs) that transpiles the app's .ts/.vue sources with
# the `typescript` package and imports `vue`; none of them build or call the
# Rust side, so no Rust toolchain / webkit stack is provisioned.
#
# Toolchain pins, read at the base sha:
#   * Node.js 20 -- .github/workflows/pr-build.yml `actions/setup-node` with
#     `node-version: 20` (no .nvmrc / engines). 20.20.2 is the newest 20.x.
#     Node 20 reached end-of-life on 2026-04-30, before this base commit
#     (2026-10-15); it is pinned anyway, deliberately, because the repo's own
#     CI (pr-build.yml and the release workflows) still pins
#     `node-version: 20` at this commit, so 20 is the era-faithful runtime the
#     graded tests were written and run against. EOL acknowledged.
#   * pnpm 9 -- same workflow, `pnpm/action-setup` `version: 9`; pinned to the
#     last 9.x release (9.15.9). src-vite/pnpm-lock.yaml is lockfileVersion 9.0.
#   * install -- `pnpm install --frozen-lockfile` in src-vite, exactly as CI.
# Tests run with the repo's own invocation (`node --test`, package.json
# `test:startup`), one file at a time, only the files the test.patch touches.

_INTERVAL = "lap_432_to_432"
# Official Docker library image, via its ECR Public mirror (same multi-arch manifest).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:20.20.2-bookworm"
_PNPM_VERSION = "9.15.9"
_APT = "git ca-certificates build-essential python3 curl"
_JS_ROOT = "src-vite"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_TEST_FILE = re.compile(r"^src-vite/tests/[^/]+\.test\.[cm]?js$")
_FILE_MARK = re.compile(r"^MSB_TEST_FILE (\S+)$")
_SUBTEST = re.compile(r"^( *)# Subtest: (.*)$")
_TAP_RESULT = re.compile(r"^( *)(not ok|ok) \d+ - (.*?)(?: # (SKIP|TODO)\b.*)?$")

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

# ---------- Section 2: PROVISION (build time, pinned by src-vite/pnpm-lock.yaml) ----------
cd /home/[[REPO]]/[[JSROOT]]
pnpm install --frozen-lockfile

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# The graded tests load `typescript` and `vue` from src-vite/node_modules and
# run under the built-in node:test runner.
node --version
pnpm --version
node --input-type=module -e "import ts from 'typescript'; import * as vue from 'vue'; if (!ts.transpile || !vue.reactive) process.exit(1); console.log('deps ok')"
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
cd [[JSROOT]]
# One node:test process per file (TAP output, file marker above each), so a
# file that fails to load cannot hide the results of the others.
status=0
while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "MSB_TEST_FILE ${f}"
    if ! node --test --test-reporter=tap "${f#[[JSROOT]]/}"; then
        status=1
    fi
done < /home/test_files.txt
exit $status
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
    return "".join(f"{f}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[JSROOT]]", _JS_ROOT)
        .replace("[[REPO]]", pr.repo)
    )


class Lap432To432ImageBase(Image):
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
    NODE_OPTIONS=--max-old-space-size=4096

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

# pnpm {_PNPM_VERSION} (CI: pnpm/action-setup version 9).
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


class Lap432To432ImageDefault(Image):
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
        return Lap432To432ImageBase(self.pr, self._config)

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


@Instance.register("julyx10", _INTERVAL)
class LAP_432_TO_432(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Lap432To432ImageDefault(self.pr, self._config)

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
        current: str | None = None
        stack: list[str] = []
        for raw in clean.splitlines():
            line = raw.rstrip()
            mark = _FILE_MARK.match(line.strip())
            if mark:
                current = mark.group(1)
                stack = []
                continue
            if current is None:
                continue
            sub = _SUBTEST.match(line)
            if sub:
                level = len(sub.group(1)) // 4
                stack = stack[:level] + [sub.group(2).strip()]
                continue
            result = _TAP_RESULT.match(line)
            if not result:
                continue
            level = len(result.group(1)) // 4
            parts = stack[:level] + [result.group(3).strip()]
            name = current + "::" + " > ".join(parts)
            if result.group(4):
                skipped_tests.add(name)
            elif result.group(2) == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)
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
