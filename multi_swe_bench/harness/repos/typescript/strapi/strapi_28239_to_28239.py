import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# strapi/strapi, PR 28239 (base commit 2026-11-03).
#
# Graded tests: the admin-side jest files the test.patch touches, run per
# workspace package with that package's own config, exactly like its
# `test:front` script (`cross-env IS_EE=true jest --config ./jest.config.front.js`).
# The Playwright e2e specs under tests/e2e need a running Strapi server and
# browsers, so they are not run.
#
# Toolchain pins, read at the base sha:
#   * Node.js 24 -- .github/workflows/tests.yml `unit_front` matrix `node: [24]`
#     (package.json engines ">=22.0.0 <=26.x.x"; .nvmrc 22 is the dev default).
#   * yarn 4.12.0 -- package.json "packageManager" + .yarnrc.yml `yarnPath`
#     (.yarn/releases/yarn-4.12.0.cjs is committed), via corepack as in CI.
#   * install -- `yarn install --immutable` (CI .github/actions/yarn-nm-install).
#   * build -- CI builds the monorepo before unit_front (.github/actions/run-build):
#     cross-package imports such as `@strapi/admin/strapi-admin` resolve to each
#     package's dist/. prepare.sh builds every package's code once; each stage
#     then rebuilds only the dist of workspace packages that (a) the applied
#     patches changed outside test files and (b) a graded package depends on,
#     so the fix stage sees the fixed dist (a no-op in the baseline/test stages).

_INTERVAL = "strapi_28239_to_28239"
# Official Docker library image, via its ECR Public mirror (same multi-arch manifest).
_NODE_IMAGE = "public.ecr.aws/docker/library/node:24.21.0-bookworm"
_APT = "git ca-certificates build-essential python3 curl"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_TEST_FILE = re.compile(r"^(packages/[^/]+/[^/]+)/.+\.test\.[cm]?[jt]sx?$")
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

# Yarn berry install policy: the base ENV sets YARN_ENABLE_IMMUTABLE_INSTALLS=false
# (QC 2B), and prepare.sh still passes an explicit `--immutable`, which wins over
# the env var. The flag is deliberate: it is the exact command of the repo's CI
# (.github/actions/yarn-nm-install/action.yml
# `yarn install --immutable --inline-builds`), it pins every dependency to the
# committed yarn.lock (no silent re-resolution to versions newer than this
# commit), and a drifted lockfile fails the image build loudly instead of
# producing a different dependency tree.
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

# ---------- Section 2: PROVISION (build time, pinned by yarn.lock) ----------
# Same install as CI (.github/actions/yarn-nm-install), lockfile immutable.
yarn install --immutable --inline-builds
# Same build CI runs before unit_front, code only (jest needs no .d.ts),
# parallelism capped for the shared builder.
yarn nx run-many --targets build:code --nx-ignore-cycles --parallel=2

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# jest, the shared front preset's environment, and the built dist of the
# admin package that the graded packages import must all resolve.
node --version
yarn --version
test -f packages/core/admin/dist/admin/index.js -o -f packages/core/admin/dist/admin/index.mjs
while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    (cd "$pkg" && test -f jest.config.front.js && yarn run -T jest --version > /dev/null \
        && node -e "require.resolve('@strapi/admin-test-utils/environment'); require.resolve('@strapi/admin/strapi-admin'); console.log('deps ok: ' + process.cwd())")
done < /home/test_pkgs.txt
echo DEPS_OK
"""

_REBUILD_JS = """'use strict';
// Rebuilds (build:code) the workspace packages whose non-test sources differ
// from HEAD and that a graded package depends on. No-op on an unpatched tree.
const fs = require('fs');
const path = require('path');
const { execSync, execFileSync } = require('child_process');
const root = process.cwd();
const graded = fs.readFileSync('/home/test_pkgs.txt', 'utf8').split('\\n').filter(Boolean);
const workspaces = execSync('yarn workspaces list --json', { encoding: 'utf8' })
  .split('\\n').filter(Boolean).map((l) => JSON.parse(l)).filter((w) => w.location !== '.');
const status = execSync('git status --porcelain --untracked-files=all', { encoding: 'utf8' })
  .split('\\n').filter(Boolean).map((l) => l.slice(3).split(' -> ').pop());
const isTest = (f) => /(^|\\/)(tests?|__tests__|__snapshots__|__mocks__)\\//.test(f) || /\\.(test|spec)\\.[cm]?[jt]sx?$/.test(f);
const changed = new Set();
for (const f of status) {
  if (isTest(f)) continue;
  const ws = workspaces.find((w) => f.startsWith(w.location + '/'));
  if (ws) changed.add(ws.name);
}
const deps = new Set();
for (const dir of graded) {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, dir, 'package.json'), 'utf8'));
  for (const k of ['dependencies', 'devDependencies', 'peerDependencies']) {
    Object.keys(pkg[k] || {}).forEach((d) => deps.add(d));
  }
}
const todo = [...changed].filter((n) => deps.has(n)).sort();
console.log('rebuild: ' + (todo.length ? todo.join(' ') : '(nothing)'));
for (const name of todo) {
  execFileSync('yarn', ['workspace', name, 'run', 'build:code'], { stdio: 'inherit' });
}
"""

_REPORT_JS = """'use strict';
// Prints one stable line per test case from a jest JSON report.
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
export IS_EE=true

cd /home/[[REPO]]
[[APPLY]]
node /home/rebuild_changed.cjs
rc=0
while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    mapfile -t files < <(grep "^${pkg}/" /home/test_files.txt | sed "s|^${pkg}/||")
    echo "=== jest ${pkg} (${#files[@]} files)"
    rm -f /tmp/msb-jest.json
    (cd "$pkg" && yarn run -T jest --config ./jest.config.front.js --ci --maxWorkers=2 --testTimeout=60000 \\
        --json --outputFile=/tmp/msb-jest.json --runTestsByPath "${files[@]}") || rc=$?
    node /home/report_json.cjs /tmp/msb-jest.json /home/[[REPO]]
done < /home/test_pkgs.txt
exit $rc
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    return list(dict.fromkeys(m.group(2) for m in _FILE_HEADER.finditer(patch)))


def _graded(pr: PullRequest) -> list[tuple[str, str]]:
    out = []
    for f in _patch_files(pr.test_patch):
        m = _TEST_FILE.match(f)
        if m:
            out.append((m.group(1), f))
    return out


def _test_files(pr: PullRequest) -> str:
    return "".join(f"{f}\n" for _, f in _graded(pr))


def _test_pkgs(pr: PullRequest) -> str:
    return "".join(f"{p}\n" for p in dict.fromkeys(p for p, _ in _graded(pr)))


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


class Strapi28239To28239ImageBase(Image):
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
    NODE_OPTIONS=--max-old-space-size=6144 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \\
    YARN_ENABLE_GLOBAL_CACHE=false \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    NX_DAEMON=false \\
    NX_NO_CLOUD=true \\
    NX_TUI=false \\
    HUSKY=0

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

# yarn 4.12.0 itself is committed (.yarnrc.yml yarnPath); corepack provides
# the `yarn` shim exactly as CI's yarn-nm-install action (`corepack enable`).
RUN corepack enable && corepack --version

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


class Strapi28239To28239ImageDefault(Image):
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
        return Strapi28239To28239ImageBase(self.pr, self._config)

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
            File(".", "test_pkgs.txt", _test_pkgs(pr)),
            File(".", "report_json.cjs", _REPORT_JS),
            File(".", "rebuild_changed.cjs", _REBUILD_JS),
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


@Instance.register("strapi", _INTERVAL)
class STRAPI_28239_TO_28239(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Strapi28239To28239ImageDefault(self.pr, self._config)

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
