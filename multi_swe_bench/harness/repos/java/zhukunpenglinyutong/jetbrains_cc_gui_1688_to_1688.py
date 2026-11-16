import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES = """#!/bin/bash
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

_NODE_REPORTER = """import path from "node:path";

export default async function* reporter(source) {
  const stacks = new Map();
  const root = process.cwd();
  for await (const ev of source) {
    const d = ev.data || {};
    const file = d.file ? path.relative(root, d.file) : "unknown";
    if (ev.type === "test:start") {
      let st = stacks.get(file);
      if (!st) {
        st = [];
        stacks.set(file, st);
      }
      st.length = d.nesting;
      st.push(d.name);
      continue;
    }
    if (ev.type !== "test:pass" && ev.type !== "test:fail") continue;
    if (d.details && d.details.type === "suite") continue;
    const st = (stacks.get(file) || []).slice(0, d.nesting);
    const parts = [file].concat(st, [d.name]);
    let status = ev.type === "test:pass" ? "PASSED" : "FAILED";
    if (d.skip !== undefined || d.todo !== undefined) status = "SKIPPED";
    yield "NODE_TESTCASE " + status + " " + parts.join(" > ").replace(/\\s+/g, " ") + "\\n";
  }
}
"""

_VITEST_REPORT = """const fs = require("fs");
const path = require("path");
const file = process.argv[2];
const root = process.argv[3];
let data;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (e) {
  console.log("VITEST_REPORT_MISSING");
  process.exit(0);
}
for (const suite of data.testResults || []) {
  const rel = path.relative(root, suite.name);
  const cases = suite.assertionResults || [];
  if (cases.length === 0 && suite.status === "failed") {
    console.log("NODE_TESTCASE FAILED " + rel);
    continue;
  }
  for (const c of cases) {
    const name = [rel].concat(c.ancestorTitles || [], [c.title]).join(" > ");
    let st = "SKIPPED";
    if (c.status === "passed") st = "PASSED";
    else if (c.status === "failed") st = "FAILED";
    console.log("NODE_TESTCASE " + st + " " + name.replace(/\\s+/g, " "));
  }
}
"""

_PREPARE = """#!/bin/bash
set -e

cd /home/@@REPO@@
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e @@SHA@@^{commit} 2>/dev/null || git fetch origin @@SHA@@
git checkout --detach @@SHA@@
test "$(git rev-parse HEAD)" = "@@SHA@@"
bash /home/check_git_changes.sh

cd /home/@@REPO@@/ai-bridge
npm ci --no-audit --no-fund || npm install --no-audit --no-fund
node -e "require('./package.json'); require.resolve('sql.js')"
cd /home/@@REPO@@/webview
npm ci --no-audit --no-fund || npm install --no-audit --no-fund
npm run prebuild
npx vitest run src/utils --reporter=dot || true
node -e "require('./package.json'); ['react', 'jsdom', '@testing-library/react'].forEach((m) => require.resolve(m))"
./node_modules/.bin/vitest --version
echo DEPS_OK
"""

_RUN_TESTS = r"""cd /home/@@REPO@@
PATCH_FILES="$(sed -nE 's#^diff --git a/(.*) b/.*$#\1#p' /home/test.patch | sort -u)"
BRIDGE=""
WEB=""
BRIDGE_MATCHED=""
WEB_MATCHED=""
BRIDGE_TOUCHED=""
WEB_TOUCHED=""
for f in $PATCH_FILES; do
  case "$f" in
    ai-bridge/*) BRIDGE_TOUCHED=1 ;;
    webview/*) WEB_TOUCHED=1 ;;
  esac
  case "$f" in
    ai-bridge/*.test.js|ai-bridge/*.test.mjs|ai-bridge/*.test.cjs)
      BRIDGE_MATCHED=1
      if [ -f "$f" ]; then BRIDGE="$BRIDGE $f"; fi ;;
    webview/src/*.test.ts|webview/src/*.test.tsx)
      WEB_MATCHED=1
      if [ -f "$f" ]; then WEB="$WEB ${f#webview/}"; fi ;;
  esac
done
if [ -z "$BRIDGE_MATCHED" ] && [ -z "$WEB_MATCHED" ]; then
  if [ -n "$BRIDGE_TOUCHED" ]; then
    BRIDGE=$(find ai-bridge -path '*/node_modules' -prune -o -type f \( -name '*.test.js' -o -name '*.test.mjs' -o -name '*.test.cjs' \) -print | sort | tr '\n' ' ')
  fi
  if [ -n "$WEB_TOUCHED" ]; then
    WEB="src"
  fi
fi
rc=0
if [ -n "$BRIDGE" ]; then
  timeout -k 30 1200 node --test --test-reporter=/home/node_reporter.mjs $BRIDGE || rc=$?
fi
cd /home/@@REPO@@/webview
if [ -n "$WEB" ]; then
  rm -f /tmp/vitest-results.json
  timeout -k 30 1200 npx vitest run $WEB --reporter=json --outputFile=/tmp/vitest-results.json || rc=$?
  node /home/vitest_report.cjs /tmp/vitest-results.json /home/@@REPO@@ || rc=$?
fi
exit $rc
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
cd /home/@@REPO@@
@@APPLY@@@@RUN_TESTS@@"""


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM @@IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    CI=true PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PUPPETEER_SKIP_DOWNLOAD=1

LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \\
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
@@GLOBAL_ENV@@

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/
@@CODE@@
@@CLEAR_ENV@@

CMD ["/bin/bash"]
"""

_PRUNE = """RUN set -eux; \\
    cd /home/@@REPO@@; \\
    test "$(git rev-parse HEAD)" = "@@SHA@@"; \\
    git checkout --detach "@@SHA@@"; \\
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
    test "$(git rev-parse HEAD)" = "@@SHA@@"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/@@REPO@@/.gitmodules ]; then \\
        cd /home/@@REPO@@ && git submodule foreach --recursive ' \\
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


def _fill(body: str, pr: PullRequest, apply: str = "") -> str:
    body = body.replace("@@APPLY@@", f"git apply --whitespace=nowarn {apply}\n" if apply else "")
    return body.replace("@@RUN_TESTS@@", _RUN_TESTS).replace("@@REPO@@", pr.repo).replace("@@SHA@@", pr.base.sha)


class JetbrainsCcGuiImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return "node:22-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = self.pr.repo
        code = f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n    cd /home/{repo} && git rev-parse HEAD >/dev/null'
        if not self.config.need_clone:
            code = f"COPY {repo} /home/{repo}"
        return (
            _BASE_DOCKERFILE.replace("@@IMAGE@@", self.dependency())
            .replace("@@ORG@@", self.pr.org)
            .replace("@@REPO@@", repo)
            .replace("@@GLOBAL_ENV@@", self.global_env)
            .replace("@@CLEAR_ENV@@", self.clear_env)
            .replace("@@CODE@@", code)
        )


class JetbrainsCcGuiImageDefault(Image):
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
        return JetbrainsCcGuiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "node_reporter.mjs", _NODE_REPORTER),
            File(".", "vitest_report.cjs", _VITEST_REPORT),
            File(".", "prepare.sh", _fill(_PREPARE, self.pr)),
            File(".", "run.sh", _fill(_RUN, self.pr)),
            File(".", "test-run.sh", _fill(_RUN, self.pr, "/home/test.patch")),
            File(".", "fix-run.sh", _fill(_RUN, self.pr, "/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{_fill(_PRUNE, self.pr)}
{self.clear_env}
"""


@Instance.register("zhukunpenglinyutong", "jetbrains_cc_gui_1688_to_1688")
class JETBRAINS_CC_GUI_1688_TO_1688(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JetbrainsCcGuiImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        re_case = re.compile(r"^NODE_TESTCASE (PASSED|FAILED|SKIPPED) (\S.*?)\s*$")

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        for line in text.splitlines():
            m = re_case.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

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
