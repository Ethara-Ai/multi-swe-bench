import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# jsdom/jsdom, PR 4009 ("replace canvas with @napi-rs/canvas").
#
# Node is pinned to the CI canvas job's `node-version: 22` (.github/workflows/
# ci.yml at 8bd17a2; package.json engines "^22.22.2 || ^24.15.0 || >=26.0.0");
# npm ci honours the shipped package-lock.json.
#
# Canvas: the base commit's CI canvas job installs `canvas@3` next to the lock,
# the fixed commit's installs `@napi-rs/canvas@^1.0.13`; neither is in the lock
# (optional peer). Both are installed here, pinned to the newest release that
# predates the base commit (canvas 3.2.3, @napi-rs/canvas 1.1.0), so the same
# environment serves the pre-fix tree (which loads `canvas`) and the post-fix
# tree (which loads `@napi-rs/canvas`).
#
# Tests run (selection derived from test.patch by /home/jsdom_select.js):
#   * the test/api files the patch touches (mocha);
#   * the web-platform-tests DIR: sections of to-run.yaml whose expectations the
#     patch edits (run-wpts.js, needs the vendored WPT submodule, Python and
#     the WPT server's host names);
#   * the to-upstream tests the patch adds or whose expectations it edits
#     (run-tuwpts.js).
# Mocha's TAP reporter prints each test's full title.

_INTERVAL = "jsdom_4009_to_4009"
# Official Docker library image, pulled through its ECR Public mirror (same
# multi-arch manifest as docker.io/library/node, without Docker Hub pull limits).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:22.23.2-bookworm"
# python3-venv/python-is-python3: the WPT runner spawns `python ./wpt.py` and
# wpt creates a venv; the cairo/pango/... -dev packages are what ci.yml installs
# for node-canvas (used when its prebuilt binary is unavailable).
_APT = (
    "git ca-certificates build-essential python3 python3-venv python-is-python3 "
    "pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev"
)
_CANVAS_PACKAGES = "canvas@3.2.3 @napi-rs/canvas@1.1.0"
_WPT_DIR = "test/web-platform-tests/tests"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TAP = re.compile(r"^(ok|not ok) \d+ (.+?)(?: # (SKIP|TODO)\b.*)?$")
_HOOK = re.compile(r'^"(before|after) (all|each)" hook')
_WPT_TITLE = re.compile(
    r"^(web-platform-tests \S+ |Local tests in web-platform-test format \(to-upstream\) )"
    r"(?:\[[^\]]*\] )+"
)

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

# ---------- Section 2: PROVISION (build time, pinned by this commit) ----------
# The web-platform-tests submodule, at exactly the commit this tree's gitlink
# names (CI checks out with `submodules: recursive`); one commit, no history.
git submodule init [[WPT]]
WPT_SHA=$(git rev-parse HEAD:[[WPT]])
WPT_URL=$(git config --get submodule.[[WPT]].url)
rm -rf [[WPT]]
mkdir -p [[WPT]]
git -C [[WPT]] init -q
git -C [[WPT]] remote add origin "$WPT_URL"
git -C [[WPT]] fetch --depth 1 --no-tags origin "$WPT_SHA"
git -C [[WPT]] checkout -q --detach FETCH_HEAD

npm ci --engine-strict --no-audit --no-fund
npm install --no-save --engine-strict --no-audit --no-fund [[CANVAS]]
# `pretest`: generate the webidl2js wrappers and other build outputs.
npm run prepare
# wpt.py creates its virtualenv (and pip-installs the lint/manifest tool
# requirements) on first use; do that now so the graded runs need no network.
(cd [[WPT]] && python3 wpt.py serve --help > /dev/null && python3 wpt.py lint --help > /dev/null && python3 wpt.py manifest --help > /dev/null)

# ---------- Section 3: HARD GATE (non-tolerant) ----------
test "$(git -C [[WPT]] rev-parse HEAD)" = "$WPT_SHA"
test -x [[WPT]]/_venv3/bin/python
node -e "require('./lib/api.js'); require.resolve('mocha-sugar-free'); require('js-yaml'); require('canvas'); require('@napi-rs/canvas'); require('./lib/generated/idl/utils.js')"
node_modules/.bin/mocha --version
node /home/jsdom_select.js api > /dev/null
echo DEPS_OK
"""

_SELECT = r""""use strict";
// Derives the graded test selection from /home/test.patch, identically in all
// three stages: it reads only the patch and the BASE commit's files (git show
// HEAD:...), never the working tree. usage: node /home/jsdom_select.js <api|wpt|tuwpt>
const fs = require("node:fs");
const { execFileSync } = require("node:child_process");

const kind = process.argv[2];
const patch = fs.readFileSync("/home/test.patch", "utf8");
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const WPT = "test/web-platform-tests/";
const TO_RUN = WPT + "to-run.yaml";
const TU_EXPECT = WPT + "to-upstream-expectations.yaml";
const TU_DIR = WPT + "to-upstream/";

// Per file: the changed lines' text, and the base-file line number each
// removal sits on / each addition is inserted before.
const files = [];
const changed = {};
const oldLines = {};
let current = null;
let oldLine = 0;
let inHunk = false;
for (const line of patch.split("\n")) {
  const header = /^diff --git a\/(\S+) b\/(\S+)$/.exec(line);
  if (header) {
    current = header[2];
    files.push(current);
    changed[current] = [];
    oldLines[current] = [];
    inHunk = false;
    continue;
  }
  if (!current) continue;
  const hunk = /^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@/.exec(line);
  if (hunk) {
    oldLine = Number(hunk[1]);
    inHunk = true;
    continue;
  }
  if (!inHunk) continue;
  if (line.startsWith("-")) {
    changed[current].push(line.slice(1));
    oldLines[current].push(oldLine);
    oldLine++;
  } else if (line.startsWith("+")) {
    changed[current].push(line.slice(1));
    oldLines[current].push(oldLine);
  } else if (line.startsWith(" ")) {
    oldLine++;
  }
}

if (kind === "api") {
  for (const f of files) {
    if (/^test\/api\/[^/]+\.js$/.test(f)) console.log(f);
  }
} else if (kind === "wpt") {
  // The DIR: sections of to-run.yaml that the patch's changed lines fall into.
  const dirs = new Set();
  if (oldLines[TO_RUN] && oldLines[TO_RUN].length) {
    const lines = execFileSync("git", ["show", `HEAD:${TO_RUN}`], { encoding: "utf8" }).split("\n");
    for (const at of oldLines[TO_RUN]) {
      for (let n = Math.min(at, lines.length); n >= 1; n--) {
        const m = /^DIR: (.+?)\s*$/.exec(lines[n - 1]);
        if (m) {
          dirs.add(m[1]);
          break;
        }
      }
    }
  }
  if (dirs.size) console.log(`^web-platform-tests (${[...dirs].sort().map(escape).join("|")}) `);
} else if (kind === "tuwpt") {
  // to-upstream test files the patch adds or edits, plus those whose
  // to-upstream-expectations.yaml entry it edits.
  const tests = new Set();
  for (const f of files) {
    if (f.startsWith(TU_DIR) && !/(^|\/)(resources|support)\//.test(f) &&
        /\.(html|htm|xhtml|svg|js)$/.test(f)) {
      tests.add(f.slice(TU_DIR.length));
    }
  }
  for (const line of changed[TU_EXPECT] || []) {
    const m = /^([^\s#"'][^:]*\.(?:html|htm|xhtml|svg|js)):/.exec(line);
    if (m) tests.add(m[1]);
  }
  if (tests.size) {
    console.log(`^Local tests in web-platform-test format \\(to-upstream\\) (\\[[^\\]]*\\] )?(${[...tests].sort().map(escape).join("|")})$`);
  }
} else {
  throw new Error(`unknown selection kind: ${kind}`);
}
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
# The WPT server's host names (ci.yml: `./wpt make-hosts-file | sudo tee -a
# /etc/hosts`). /etc/hosts belongs to the running container, so it is written
# here rather than at build time.
grep -q 'web-platform\\.test' /etc/hosts || ./[[WPT]]/wpt make-hosts-file >> /etc/hosts

api=$(node /home/jsdom_select.js api)
wpt=$(node /home/jsdom_select.js wpt)
tuwpt=$(node /home/jsdom_select.js tuwpt)
echo "api tests: ${api}"
echo "wpt grep: ${wpt}"
echo "tuwpt grep: ${tuwpt}"

# Three mocha processes (each WPT runner starts its own server from a root
# hook); every one always runs, and the script exits non-zero if any failed.
# --exit: do not wait on handles a crashed or hung WPT server child leaves open.
status=0
if [ -n "$api" ]; then
    node_modules/.bin/mocha --exit --reporter tap $api || status=1
fi
if [ -n "$wpt" ]; then
    node_modules/.bin/mocha --exit --reporter tap --grep "$wpt" test/web-platform-tests/run-wpts.js || status=1
fi
if [ -n "$tuwpt" ]; then
    node_modules/.bin/mocha --exit --reporter tap --grep "$tuwpt" test/web-platform-tests/run-tuwpts.js || status=1
fi
exit $status
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[REPO]]", pr.repo)
        .replace("[[WPT]]", _WPT_DIR)
        .replace("[[CANVAS]]", _CANVAS_PACKAGES)
    )


class Jsdom4009To4009ImageBase(Image):
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


class Jsdom4009To4009ImageDefault(Image):
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
        return Jsdom4009To4009ImageBase(self.pr, self._config)

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
            File(".", "jsdom_select.js", _SELECT),
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


@Instance.register("jsdom", _INTERVAL)
class JSDOM_4009_TO_4009(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Jsdom4009To4009ImageDefault(self.pr, self._config)

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
            match = _TAP.match(raw.rstrip())
            if not match:
                continue
            outcome, title, directive = match.groups()
            if _HOOK.match(title):
                continue
            # A WPT title carries its current expectation ("[expected fail: ...] ",
            # "[needs-canvas] "), which is exactly what the test patch edits;
            # drop it so a test keeps one name across the three stages.
            name = _WPT_TITLE.sub(r"\1", title.strip())
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
