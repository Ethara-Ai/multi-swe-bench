import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# shipshapecode/shepherd, PR 3557.
#
# Toolchain pins at cd3042b: .tool-versions `nodejs 24.14.1` / `pnpm 11.21.0`,
# package.json "packageManager": "pnpm@11.21.0"; pnpm installs from the shipped
# pnpm-lock.yaml (cypress 15.21.0, vitest 4.1.11).
#
# Tests the patch touches, all under shepherd.js/:
#   * test/unit/**/*.spec.js       -> vitest (happy-dom), verbose reporter
#   * test/cypress/integration/*.cy.js -> Cypress against the http-server the
#     repo's `test:cy:ci` script starts on :9002, with the TAP reporter.
# CI runs Cypress with `--browser chrome`; Google Chrome has no linux/arm64
# build, so Cypress' bundled Electron (headless, under Cypress' own Xvfb) is
# used instead: it ships in the Cypress binary for both linux-x64 and
# linux-arm64. The run scripts rebuild dist/ (rollup) after applying the
# patches, because the Cypress example pages load ../../../dist/.

_INTERVAL = "shepherd_3557_to_3557"
# Official Docker library image, pulled through its ECR Public mirror (same
# multi-arch manifest as docker.io/library/node, without Docker Hub pull limits).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:24.14.1-bookworm"
# Cypress' documented Debian prerequisites (Electron + Xvfb).
_APT = (
    "git ca-certificates build-essential python3 "
    "libgtk2.0-0 libgtk-3-0 libgbm1 libnotify4 libnss3 libxss1 libasound2 "
    "libxtst6 xauth xvfb fonts-liberation"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_UNIT_FILE = re.compile(r"^shepherd\.js/(test/unit/.+\.(?:spec|test)\.[cm]?[jt]s)$")
_CY_FILE = re.compile(r"^shepherd\.js/(test/cypress/integration/.+\.cy\.[cm]?[jt]sx?)$")
_VITEST_RESULT = re.compile(
    r"^\s*([✓✔×✗✖↓])\s+"
    r"(test/\S+\.(?:spec|test)\.[cm]?[jt]s > .+?)"
    r"(?:\s+\(retry x\d+\))?(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
)
_CY_RUNNING = re.compile(r"Running:\s+(\S+\.cy\.[cm]?[jt]sx?)\s+\(\d+ of \d+\)")
_TAP = re.compile(r"^(ok|not ok) \d+ (.+?)(?: # (SKIP|TODO)\b.*)?$")
_HOOK = re.compile(r'^"(before|after) (all|each)" hook')

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

# ---------- Section 2: PROVISION (build time, pinned by this commit's lockfile) ----------
PNPM_VERSION=$(node -p "require('./package.json').packageManager.split('@')[1]")
npm install -g "pnpm@${PNPM_VERSION}"
# The root `prepare` script builds shepherd.js (rollup) as part of the install.
pnpm install --frozen-lockfile
# Cypress' postinstall downloads the binary for this architecture; `cypress
# install` is the repo's own `cypress:install` script and is a no-op when the
# binary is already cached.
pnpm -F shepherd.js exec cypress install

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# Cypress verifies that its Electron starts under Xvfb; the unit runner and
# the built library must resolve too.
pnpm -F shepherd.js exec cypress verify
pnpm -F shepherd.js exec vitest --version
test -f shepherd.js/dist/js/shepherd.mjs
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
unit=()
cy=()
while IFS= read -r f; do
    [ -n "$f" ] && [ -f "shepherd.js/$f" ] || continue
    case "$f" in
        test/cypress/*) cy+=("$f") ;;
        *) unit+=("$f") ;;
    esac
done < /home/test_files.txt
echo "unit specs: ${unit[*]}"
echo "cypress specs: ${cy[*]}"

cd shepherd.js
# The Cypress example pages load ../../../dist/, so rebuild it from this tree.
pnpm build

# Both suites always run; the script exits non-zero if either failed.
status=0
if [ "${#unit[@]}" -gt 0 ]; then
    pnpm exec vitest run --reporter=verbose "${unit[@]}" || status=1
fi
if [ "${#cy[@]}" -gt 0 ]; then
    spec=$(IFS=,; echo "${cy[*]}")
    pnpm exec start-server-and-test 'http-server . -p 9002 --silent' http://127.0.0.1:9002 \\
        "cypress run --config-file test/cypress/cypress.config.js --spec ${spec} --reporter tap" || status=1
fi
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
    keep = []
    for f in _patch_files(pr.test_patch):
        match = _UNIT_FILE.match(f) or _CY_FILE.match(f)
        if match:
            keep.append(match.group(1))
    return "".join(f"{f}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


class Shepherd3557To3557ImageBase(Image):
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
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    CYPRESS_CACHE_FOLDER=/root/.cache/Cypress \\
    HUSKY=0 \\
    CI=true

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

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    node --version && npm --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Shepherd3557To3557ImageDefault(Image):
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
        return Shepherd3557To3557ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
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


@Instance.register("shipshapecode", _INTERVAL)
class SHEPHERD_3557_TO_3557(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Shepherd3557To3557ImageDefault(self.pr, self._config)

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
        spec: str | None = None
        for raw in clean.splitlines():
            line = raw.rstrip()
            running = _CY_RUNNING.search(line)
            if running:
                spec = running.group(1)
                continue
            match = _VITEST_RESULT.match(line)
            if match:
                mark, name = match.group(1), match.group(2).strip()
                if mark in "\u2713\u2714":
                    passed_tests.add(name)
                elif mark == "\u2193":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue
            match = _TAP.match(line.strip())
            if match and spec:
                outcome, title, directive = match.groups()
                if _HOOK.match(title):
                    continue
                name = f"test/cypress/integration/{spec} > {title.strip()}"
                if directive:
                    skipped_tests.add(name)
                elif outcome == "ok":
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
