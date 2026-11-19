import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# garrytan/gstack, PRs 3738 and 3770 (base commits 2026-11-01 .. 2026-11-02).
#
# Toolchain pins, read at both base shas:
#   * bun 1.4.2 -- every `oven-sh/setup-bun` step in .github/workflows/free-tests.yml
#     (`bun-version: 1.4.2`) and package.json `engines.bun: ">=1.4.2"`.
#   * install -- `bun install --frozen-lockfile --ignore-scripts`, exactly as CI,
#     honouring the committed bun.lock.
#   * Node.js is not pinned by the repo (bun is the runtime); the Node LTS image
#     only supplies git/python3/build tooling, which bin/ helpers shell out to.
# Tests are `bun test` files under test/; only the *.test.ts files the PR's
# test.patch touches are run (golden fixtures arrive through test.patch).

_INTERVAL = "gstack_3770_to_3738"
# Official Docker library image, via its ECR Public mirror (same multi-arch manifest).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:22.23.2-bookworm"
_BUN_VERSION = "1.4.2"
_APT = "git ca-certificates build-essential python3 curl unzip zsh jq"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_TEST_FILE = re.compile(r"\.test\.[cm]?[jt]sx?$")
_BUN_FILE = re.compile(r"^(\S+\.test\.[cm]?[jt]sx?):$")
_BUN_RESULT = re.compile(r"^\((pass|fail|skip|todo)\) (.+?)(?: \[[0-9.]+m?s\])?$")
# bun repeats every failure after "N tests failed:"; those lines carry no file
# header of their own, so parsing stops attributing results there.
_BUN_SUMMARY = re.compile(r"^\d+ tests? (failed|skipped|todo):?$")

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

# ---------- Section 2: PROVISION (build time, pinned by this commit's bun.lock) ----------
# Same install CI runs (free-tests.yml): frozen lockfile, no lifecycle scripts
# (playwright/onnxruntime postinstall downloads are not needed by these tests).
bun install --frozen-lockfile --ignore-scripts

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# The runner and the runtime deps the graded test files import must resolve.
bun --version
bun -e "require('diff'); require('marked'); require.resolve('@types/bun/package.json'); console.log('deps ok')"
bun test --help > /dev/null
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
mapfile -t files < /home/test_files.txt
bun test "${files[@]}" 2>&1
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in _FILE_HEADER.finditer(patch)))


def _test_files(pr: PullRequest) -> str:
    keep = [f for f in _patch_files(pr.test_patch) if _TEST_FILE.search(f)]
    return "".join(f"./{f}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


class Gstack3770To3738ImageBase(Image):
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
    BUN_INSTALL=/usr/local \\
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

# bun {_BUN_VERSION} (CI pin), official release zip selected by TARGETARCH.
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) bun_pkg=bun-linux-x64-baseline ;; \\
        arm64) bun_pkg=bun-linux-aarch64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v{_BUN_VERSION}/${{bun_pkg}}.zip"; \\
    unzip -q /tmp/bun.zip -d /tmp; \\
    install -m 0755 "/tmp/${{bun_pkg}}/bun" /usr/local/bin/bun; \\
    ln -sf /usr/local/bin/bun /usr/local/bin/bunx; \\
    rm -rf /tmp/bun.zip "/tmp/${{bun_pkg}}"; \\
    test "$(bun --version)" = "{_BUN_VERSION}"

# Tests init temporary repos and commit into them (CI configures the same identity).
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


class Gstack3770To3738ImageDefault(Image):
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
        return Gstack3770To3738ImageBase(self.pr, self._config)

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


@Instance.register("garrytan", _INTERVAL)
class GSTACK_3770_TO_3738(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Gstack3770To3738ImageDefault(self.pr, self._config)

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
        current: Optional[str] = None
        for raw in clean.splitlines():
            line = raw.strip()
            header = _BUN_FILE.match(line)
            if header:
                current = header.group(1).removeprefix("./")
                continue
            if _BUN_SUMMARY.match(line):
                current = None
                continue
            result = _BUN_RESULT.match(line)
            if not result or current is None:
                continue
            name = f"{current}::{result.group(2)}"
            status = result.group(1)
            if status == "pass":
                passed_tests.add(name)
            elif status == "fail":
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
