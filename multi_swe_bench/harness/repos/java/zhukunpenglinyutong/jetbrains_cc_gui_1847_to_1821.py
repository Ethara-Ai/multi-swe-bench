import html
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "zhukunpenglinyutong"
_REPO = "jetbrains-cc-gui"
_INTERVAL = "jetbrains_cc_gui_1847_to_1821"

# The credited tests of this shard are JavaScript only:
#   - ai-bridge/**/*.test.js  -> Node's built-in runner (node:test)
#   - webview/src/**/*.test.tsx -> vitest (happy-dom environment)
# Neither needs the JDK / IntelliJ Gradle build, so no JDK is installed.
# Runtime: .github/workflows/tests.yml (both base commits) pins
# `actions/setup-node` node-version '22.12.0' for the ai-bridge and webview
# jobs. The Docker official image is referenced through its ECR Public mirror
# (same multi-arch manifest as docker.io/library/node) to avoid Docker Hub's
# anonymous pull rate limit.
_NODE_IMAGE = "public.ecr.aws/docker/library/node:22.12.0-bookworm"
_NODE_VERSION = "v22.12.0"

_BASE_APT = "git ca-certificates curl"

_TEST_TIMEOUT = "timeout -k 30 1500"

_CHECK_GIT_CHANGES = r"""#!/bin/bash
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

# node:test reporter: one line per test case, named
# "<repo-relative file> > <describe...> > <test>", so names are unique across
# files and stable across stages (no durations / counters).
_EMIT_TESTCASES_MJS = r"""import path from 'node:path';

const repoDir = process.cwd();
const clean = (v) => String(v === undefined || v === null ? '' : v).replace(/\s+/g, ' ').trim();

export default async function* reporter(source) {
    const stacks = new Map();

    for await (const event of source) {
        const data = event.data || {};
        const file = data.file || '';

        if (event.type === 'test:start') {
            if (!stacks.has(file)) stacks.set(file, []);
            const stack = stacks.get(file);
            stack.length = data.nesting;
            stack[data.nesting] = data.name;
            continue;
        }

        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        if ((data.details || {}).type === 'suite') continue;

        const rel = (path.relative(repoDir, file) || file).split(path.sep).join('/');
        const parts = [rel, ...(stacks.get(file) || []).slice(0, data.nesting).map(clean)].filter(Boolean);

        const title = clean(data.name);
        if (title && title !== rel && title !== file) parts.push(title);

        const status = event.type === 'test:fail' ? 'FAILED'
            : data.skip || data.todo ? 'SKIPPED' : 'PASSED';
        yield 'TESTCASE ' + status + ' ' + parts.join(' > ') + '\n';
    }
}
"""

_APPLY_FN = r"""_apply_patches() {
    local _ex
    _ex="$(awk '/^diff --git /{p=$NF; sub(/^b\//, "", p)} /^Binary files .* differ$/{print "--exclude=" p}' "$@" | sort -u)"
    git apply --whitespace=nowarn $_ex "$@"
}
"""

# Test selection: only the test files test.patch touches that exist at this
# stage (run.sh sees just the pre-existing ones).
_DERIVE_TARGETS = r"""BRIDGE_TESTS=""
WEB_TESTS=""
for _f in $(sed -nE 's#^diff --git a/.* b/(.*)$#\1#p' /home/test.patch | sort -u); do
    [ -f "$_f" ] || continue
    case "$_f" in
        ai-bridge/*.test.js|ai-bridge/*.test.mjs) BRIDGE_TESTS="$BRIDGE_TESTS $_f" ;;
        webview/src/*.test.ts|webview/src/*.test.tsx) WEB_TESTS="$WEB_TESTS ${_f#webview/}" ;;
    esac
done
echo "selected: ai-bridge=[${BRIDGE_TESTS# }] webview=[${WEB_TESTS# }]"
"""

# ai-bridge runs from the repository root, exactly like the CI job
# (`node --test "ai-bridge/**/*.test.js"`); webview runs `vitest run` from
# webview/ as `npm run test` does (the tsc typecheck half is not a test).
# A non-zero runner exit only means "some test failed" and is reported
# through the per-test lines, so the stage does not abort on it before the
# second runner has run; the collected status is propagated at the end
# (`exit $RC`) so a runner that crashed or could not start fails the stage.
_RUN_TARGETS = r"""if [ -z "$BRIDGE_TESTS" ] && [ -z "$WEB_TESTS" ]; then
    echo "NO_TESTS_AT_THIS_STAGE"
    exit 0
fi

RC=0
if [ -n "$BRIDGE_TESTS" ]; then
    @@TEST_TIMEOUT@@ node --test --test-concurrency=2 --test-timeout=60000 \
        --test-reporter=/home/emit_testcases.mjs --test-reporter-destination=stdout \
        $BRIDGE_TESTS || RC=$?
fi

if [ -n "$WEB_TESTS" ]; then
    rm -f /tmp/vitest-junit.xml
    cd @@REPO_DIR@@/webview
    @@TEST_TIMEOUT@@ ./node_modules/.bin/vitest run --maxWorkers=2 --minWorkers=1 \
        --reporter=default --reporter=junit --outputFile.junit=/tmp/vitest-junit.xml \
        $WEB_TESTS || RC=$?
    cd @@REPO_DIR@@
    echo "VITEST_JUNIT_BEGIN"
    if [ -f /tmp/vitest-junit.xml ]; then cat /tmp/vitest-junit.xml; fi
    echo "VITEST_JUNIT_END"
fi

echo "TEST_RUNNER_EXIT_CODE=$RC"
exit $RC
"""

_PREPARE = r"""#!/bin/bash
set -eo pipefail
export CI=true

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd @@REPO_DIR@@

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "@@SHA@@"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "@@SHA@@"

# ---------- Section 2: PROVISION (build time, pinned by this commit's lockfiles) ----------
# Both packages ship a package-lock.json (lockfileVersion 3); `npm ci` installs
# exactly that resolution, as the CI workflow does.
cd @@REPO_DIR@@/ai-bridge
npm ci --no-audit --no-fund
cd @@REPO_DIR@@/webview
npm ci --no-audit --no-fund
# src/version/version.ts is generated from build.gradle and gitignored; the
# CI webview job runs `npm run prebuild` before vitest because tests import it.
npm run prebuild
cd @@REPO_DIR@@

# ---------- Section 3: HARD GATE (non-tolerant) ----------
test "$(node --version)" = "@@NODE_VERSION@@"
test -f @@REPO_DIR@@/webview/src/version/version.ts
cd @@REPO_DIR@@/ai-bridge
node --input-type=module -e "await import('sql.js'); import('node:test')"
cd @@REPO_DIR@@/webview
./node_modules/.bin/vitest --version
node --input-type=module -e "await import('happy-dom'); await import('@testing-library/react'); await import('react-dom')"
cd @@REPO_DIR@@
test "$(git rev-parse HEAD)" = "@@SHA@@"
echo "DEPS_OK"
"""

_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

cd @@REPO_DIR@@

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_TEST_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@
_apply_patches /home/test.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_FIX_RUN = r"""#!/bin/bash
set -eo pipefail
export CI=true

@@APPLY_FN@@

cd @@REPO_DIR@@
_apply_patches /home/test.patch /home/fix.patch

@@DERIVE_TARGETS@@

@@RUN_TARGETS@@
"""

_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    no_proxy=${no_proxy} \
    NO_PROXY=${NO_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \
    npm_config_cafile=${CA_CERT_PATH} \
    npm_config_update_notifier=false \
    NO_COLOR=1 \
    FORCE_COLOR=0
@@GLOBAL_ENV@@
LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \
    @@APT@@ \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    test "$(node --version)" = "@@NODE_VERSION@@"; \
    npm --version; \
    git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@
@@CLEAR_ENV@@
CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = r"""FROM @@BASE_IMAGE@@
@@GLOBAL_ENV@@
@@COPY@@

RUN bash /home/prepare.sh

RUN set -eux; \
    cd @@REPO_DIR@@; \
    git checkout --detach "@@SHA@@"; \
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
    test "$(git rev-parse HEAD)" = "@@SHA@@"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test -z "$(git reflog)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f @@REPO_DIR@@/.gitmodules ]; then \
        cd @@REPO_DIR@@ && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi
@@CLEAR_ENV@@
"""


def _render(body: str, values: dict) -> str:
    for key, value in values.items():
        body = body.replace("@@%s@@" % key, value)
    return body


def _optional_block(block: str) -> str:
    return "\n%s\n" % block if block else ""


class JetbrainsCcGui1847To1821ImageBase(Image):
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
        repo = self.pr.repo
        if self.config.need_clone:
            code = (
                'RUN git clone "${REPO_URL}" /home/%s && \\\n'
                "    cd /home/%s && git rev-parse HEAD >/dev/null"
            ) % (repo, repo)
        else:
            code = "COPY %s /home/%s" % (repo, repo)

        return _render(
            _BASE_DOCKERFILE,
            {
                "IMAGE": self.dependency(),
                "ORG": self.pr.org,
                "REPO": repo,
                "APT": _BASE_APT,
                "NODE_VERSION": _NODE_VERSION,
                "CODE": code,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            },
        )


class JetbrainsCcGui1847To1821ImageDefault(Image):
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
        return JetbrainsCcGui1847To1821ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict:
        return {
            "REPO_DIR": "/home/%s" % self.pr.repo,
            "SHA": self.pr.base.sha,
            "NODE_VERSION": _NODE_VERSION,
            "TEST_TIMEOUT": _TEST_TIMEOUT,
        }

    def _script(self, body: str) -> str:
        body = _render(
            body,
            {
                "APPLY_FN": _APPLY_FN,
                "DERIVE_TARGETS": _DERIVE_TARGETS,
                "RUN_TARGETS": _RUN_TARGETS,
            },
        )
        return _render(body, self._values())

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "emit_testcases.mjs", _EMIT_TESTCASES_MJS),
            File(".", "prepare.sh", self._script(_PREPARE)),
            File(".", "run.sh", self._script(_RUN)),
            File(".", "test-run.sh", self._script(_TEST_RUN)),
            File(".", "fix-run.sh", self._script(_FIX_RUN)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())
        values = self._values()
        values.update(
            {
                "BASE_IMAGE": "%s:%s" % (base.image_name(), base.image_tag()),
                "COPY": copy_commands,
                "GLOBAL_ENV": _optional_block(self.global_env),
                "CLEAR_ENV": _optional_block(self.clear_env),
            }
        )
        return _render(_PR_DOCKERFILE, values)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_NODE_CASE = re.compile(r"^TESTCASE (PASSED|FAILED|SKIPPED) (.+?)\s*$")
_JUNIT_BLOCK = re.compile(r"VITEST_JUNIT_BEGIN\n(.*?)\nVITEST_JUNIT_END", re.DOTALL)
_TESTCASE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
_NAME = re.compile(r'\bname="([^"]*)"')
_CLASSNAME = re.compile(r'\bclassname="([^"]*)"')


def _parse(test_log: str) -> TestResult:
    clean = _ANSI.sub("", test_log).replace("\r\n", "\n")

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    for line in clean.split("\n"):
        m = _NODE_CASE.match(line.strip())
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "FAILED":
            failed.add(name)
        elif status == "SKIPPED":
            skipped.add(name)
        else:
            passed.add(name)

    for block in _JUNIT_BLOCK.findall(clean):
        for m in _TESTCASE.finditer(block):
            attrs = m.group(1)
            nm = _NAME.search(attrs)
            cn = _CLASSNAME.search(attrs)
            if not nm:
                continue
            cls = html.unescape(cn.group(1)).strip() if cn else ""
            title = " ".join(html.unescape(nm.group(1)).split())
            test_id = "webview/%s > %s" % (cls, title) if cls else "webview/%s" % title
            inner = m.group(3) or ""
            if m.group(2) == "/>":
                passed.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed.add(test_id)
            elif "<skipped" in inner:
                skipped.add(test_id)
            else:
                passed.add(test_id)

    passed -= failed
    skipped -= failed
    passed -= skipped

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register(_ORG, _INTERVAL)
class JETBRAINS_CC_GUI_1847_TO_1821(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return JetbrainsCcGui1847To1821ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse(test_log)
