import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# plotly/plotly.js, PR 7945.
#
# Node is pinned by .nvmrc (22.23.0) at ba74af7, which ci.yml reads through
# `node-version-file: '.nvmrc'`; npm ci honours the shipped package-lock.json
# (karma 6.4.4, jasmine-core 3.5.0, karma-spec-reporter 0.0.36).
#
# Tests: the jasmine files the test patch touches (test/jasmine/tests/*_test.js,
# passed to karma.conf.js by suite name, which re-appends "_test.js"),
# run as CI does (`npm run test-jasmine -- <suite> --nowatch` under xvfb-run,
# CI=true so the spec reporter lists every spec and @noCI specs are skipped)
# in Debian's Chromium. The image-baseline PNGs in the test patch are binary
# hunks and are excluded from `git apply`; the image (pixel) tests that use
# them are not run.

_INTERVAL = "plotly_js_7945_to_7945"
# Official Docker library image, pulled through its ECR Public mirror (same
# multi-arch manifest as docker.io/library/node, without Docker Hub pull limits).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:22.23.0-bookworm"
# chromium + xvfb/xauth: karma's browser (CI: xvfb-run + Chrome); the cairo/...
# -dev packages back node-canvas (a devDependency) when its prebuilt binary is
# unavailable.
_APT = (
    "git ca-certificates build-essential python3 pkg-config "
    "libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev "
    "chromium xvfb xauth fonts-liberation"
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_JASMINE_FILE = re.compile(r"^test/jasmine/tests/[^/]+_test\.js$")
_SPEC_LINE = re.compile(r"^( *)([✓✔✗✖-]) (.+?)(?: \(\d+(?:\.\d+)? ?(?:ms|secs?)\))?(?: \(skipped\))?\s*$")
_SUITE_LINE = re.compile(r"^((?:  )+)(\S.*?)\s*$")

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
npm ci --no-audit --no-fund
# ci.yml `install-and-cibuild` runs pretest (creates the test image folders).
# Its `cibuild` (preprocess + bundle) only produces dist/ bundles; the jasmine
# suites are bundled from src/ by karma-esbuild, and the build/plotcss.js and
# lib/ files they import are tracked at this commit, so it is not needed.
npm run pretest

# ---------- Section 3: HARD GATE (non-tolerant) ----------
node -e "require('karma'); require('karma-esbuild'); require('karma-jasmine'); require('karma-spec-reporter'); require('karma-chrome-launcher'); require('jasmine-core'); require('esbuild')"
test -f build/plotcss.js
"$CHROME_BIN" --version
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
suites=()
while IFS= read -r f; do
    [ -n "$f" ] && [ -f "$f" ] && suites+=("$(basename "$f" _test.js)")
done < /home/test_files.txt
echo "jasmine suites: ${suites[*]}"
xvfb-run --auto-servernum --server-args="-screen 0 1280x1024x24" \\
    npm run test-jasmine -- "${suites[@]}" --nowatch
"""

_APPLY_TEST = "git apply --whitespace=nowarn --exclude='*.png' /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn --exclude='*.png' /home/test.patch\n"
    "git apply --whitespace=nowarn --exclude='*.png' /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in _FILE_HEADER.finditer(patch)))


def _test_files(pr: PullRequest) -> str:
    keep = [f for f in _patch_files(pr.test_patch) if _JASMINE_FILE.match(f)]
    return "".join(f"{f}\n" for f in keep)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


class PlotlyJs7945To7945ImageBase(Image):
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
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CHROME_BIN=/usr/local/bin/chromium-karma \\
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

# karma-chrome-launcher starts $CHROME_BIN; Debian's chromium exists for both
# amd64 and arm64 (Google Chrome, which CI uses, is amd64-only). Containers run
# as root, where Chromium refuses to start without --no-sandbox.
RUN printf '%s\\n' '#!/bin/sh' 'exec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"' \\
        > /usr/local/bin/chromium-karma && \\
    chmod +x /usr/local/bin/chromium-karma && \\
    /usr/local/bin/chromium-karma --version

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


class PlotlyJs7945To7945ImageDefault(Image):
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
        return PlotlyJs7945To7945ImageBase(self.pr, self._config)

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
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
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
            git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("plotly", _INTERVAL)
class PLOTLY_JS_7945_TO_7945(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PlotlyJs7945To7945ImageDefault(self.pr, self._config)

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
        # karma-spec-reporter prints each suite title once, indented two spaces
        # per nesting level, and each spec one level deeper with its mark.
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        suites: list[str] = []
        for raw in clean.splitlines():
            line = raw.rstrip()
            spec = _SPEC_LINE.match(line)
            if spec and len(spec.group(1)) >= 4 and len(spec.group(1)) % 2 == 0:
                depth = len(spec.group(1)) // 2 - 1
                name = " > ".join(suites[:depth] + [spec.group(3).strip()])
                mark = spec.group(2)
                if mark in "\u2713\u2714":
                    passed_tests.add(name)
                elif mark == "-":
                    skipped_tests.add(name)
                else:
                    failed_tests.add(name)
                continue
            suite = _SUITE_LINE.match(line)
            if suite and "\t" not in raw and not line.strip().startswith(("at ", "Expected", "Error")):
                depth = len(suite.group(1)) // 2 - 1
                suites = suites[:depth] + [suite.group(2)]
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
