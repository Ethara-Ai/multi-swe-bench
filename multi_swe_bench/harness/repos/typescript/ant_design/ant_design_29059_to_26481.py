from __future__ import annotations

from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log

# Shard 26481..29059 == ant-design v4 (Aug 2020 - Jan 2021). node:16 is what the
# overlapping ant_design_30655_to_25074 shard uses; the buildpack-deps node image ships
# git, so no apt layer is needed.
NODE_IMAGE = "node:16"

# Shared base tag fixed by the interval endpoints (this config's own name), NOT by an
# enumerated PR list -- every PR resolves to the SAME base image regardless of subset.
_BASE_IMAGE_TAG = "base-29059_to_26481"

# Graded jest command, scoped to ONLY the test files the test.patch touches (derived
# identically across run/test/fix -> consistent command, P7). ant-design's own jest config
# is .jest.js. parse_jest_log reads the --verbose output.
TEST_CMD = (
    "FILES=$(sed -n 's#^diff --git a/\\([^ ]*\\) b/.*#\\1#p' /home/test.patch); "
    # test/spec files test.patch edits directly
    "DIRECT=$(echo \"$FILES\" | grep -E '__tests__/.*\\.(test|spec)\\.[jt]sx?$' | grep -v '\\.snap$'); "
    # test files whose SNAPSHOT test.patch updates (it rewrites the expected output, so that
    # test IS a graded target) -> map __snapshots__/<file>.snap back to <file>
    "SNAP=$(echo \"$FILES\" "
    "| grep -E '__tests__/__snapshots__/.*\\.(test|spec)\\.[jt]sx?\\.snap$' "
    "| sed 's#__snapshots__/##; s#\\.snap$##'); "
    "PAT=$(printf '%s\\n%s\\n' \"$DIRECT\" \"$SNAP\" | grep -v '^$' | sort -u "
    "| sed 's/[.[()*^$+?{}|\\\\]/\\\\&/g' | paste -sd '|' -); "
    'echo "jest target pattern: ${PAT:-<none>}"; '
    "npx jest --config .jest.js --cache=false --verbose --ci "
    '--testPathPattern "${PAT:-a^}" --passWithNoTests || true'
)


class _AntDesignImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_IMAGE_TAG

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        fetch = f'RUN git -C /home clone "${{REPO_URL}}" {repo}'

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

WORKDIR /home/

{fetch}

CMD ["/bin/bash"]
"""


class _AntDesignImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return _AntDesignImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        base_sha = self.pr.base.sha

        prepare_sh = """#!/bin/bash
set -e

cd /home/{repo}

BASE_COMMIT="${{BASE_COMMIT:-{base_sha}}}"

# Pin to the base commit (the dockerfile already detached here; defensive re-assert) and
# prove the tree is clean BEFORE any dependency work.
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

# --- era dependency install (repo ships a package-lock.json; npm install honours it but can
# still drift transitives that break under this node -> era_fixups.sh re-pins them), retried. ---
ok=0
for attempt in 1 2 3 4; do
    if npm install --legacy-peer-deps --no-audit --no-fund; then
        ok=1; echo "prepare: npm install succeeded on attempt $attempt"; break
    fi
    # A network-aborted install (ECONNRESET) leaves node_modules half-written -- a package dir
    # with an empty "version" that makes every LATER npm command die with "Invalid Version: ".
    # Wipe the partial tree so the retry starts clean instead of inheriting the corruption.
    echo "prepare: npm install attempt $attempt failed; wiping partial tree + retrying in 15s"
    rm -rf node_modules 2>/dev/null || true
    sleep 15
done
if [ "$ok" -ne 1 ]; then
    echo "prepare: npm install FAILED after 4 attempts"; exit 1
fi

# Apply the era-correct dependency fixups (babel-jest<->jest major alignment, cheerio pin,
# parse5 removal, jsdom pin, .jest.js ESM transform list). Idempotent + reused post-patch.
bash /home/era_fixups.sh
npm run version || true

# --- HARD GATE: the checked-out tree must load AND the era pins must have taken, else fail
# at BUILD (never silently produce a broken image). ---
node -e "require('./package.json'); console.log('DEPS_OK')"
node -e "var v=require('./node_modules/cheerio/package.json').version; if(v!=='1.0.0-rc.10'){{throw new Error('cheerio '+v)}} console.log('CHEERIO_OK '+v)"
node -e "var cp=require('./node_modules/@ant-design/tools/lib/jest/codePreprocessor.js'); if(typeof cp.process!=='function'){{throw new Error('codePreprocessor.process missing')}} console.log('CODEPREP_OK')"
node --version
npm --version
""".format(repo=repo, base_sha=base_sha)

        era_fixups_sh = """#!/bin/bash
# Era-correct dependency fixups for the ant-design 26481..29059 shard. Idempotent: run once in
# prepare.sh after the initial install, and again after any post-patch reinstall (a fix.patch
# that bumps a dependency in package.json triggers a reinstall that can re-drift these).
# Must be run from the repo root (/home/<repo>).
set -u

# (1) jsdom / nwsapi pins (era-correct DOM stack for this node).
npm install --no-save --legacy-peer-deps jsdom@20.0.3 nwsapi@2.2.16 2>/dev/null || true

# (2) Force cheerio@1.0.0-rc.10 at top level. enzyme resolves cheerio from the TOP-LEVEL
# node_modules; the era cheerio 1.2.0 pulls parse5-parser-stream (node: protocol import) that
# breaks on this node. The install is retried + verified (never swallowed by `|| true`).
CH=$(node -e "try{console.log(require('./node_modules/cheerio/package.json').version)}catch(e){console.log('none')}" 2>/dev/null || echo none)
if [ "$CH" != "1.0.0-rc.10" ]; then
  find node_modules -path "*/node_modules/cheerio" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null || true
  cok=0
  for attempt in 1 2 3 4; do
    # --no-package-lock: add cheerio WITHOUT reconciling the whole lockfile/tree, so a stray
    # bad entry elsewhere can't abort this install with "Invalid Version: ".
    if npm install --no-save --no-package-lock --legacy-peer-deps cheerio@1.0.0-rc.10 && [ -f node_modules/cheerio/package.json ]; then
      cok=1; echo "era_fixups: cheerio installed on attempt $attempt"; break
    fi
    echo "era_fixups: cheerio attempt $attempt failed/missing; retry in 10s"; sleep 10
  done
  if [ "$cok" -ne 1 ]; then echo "era_fixups: cheerio@1.0.0-rc.10 FAILED to install"; exit 1; fi
fi
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {} + 2>/dev/null || true

# (3) Ensure the ESM-only deps the tests import are transformed by babel-jest (.jest.js).
node << 'PATCHEOF' || true
const fs = require('fs');
try {
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {
    if (!c.includes("'" + m + "'")) {
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }
  }
  if (changed) { fs.writeFileSync('.jest.js', c); console.log('era_fixups: patched .jest.js ESM modules'); }
} catch(e) { console.log('era_fixups: no .jest.js to patch'); }
PATCHEOF

# (4) Fix the @ant-design/tools <-> babel-jest arity mismatch (the "cwd" crash).
# tools >=13's codePreprocessor.js is internally split: it does
#   const { createTransformer } = require('babel-jest').default;   // needs babel-jest 27
# yet forwards the transform as
#   babelJest.process(src, fileName, config, transformOptions);    // jest<27 4-arg shape
# Under jest 26, `config` here is the project config; babel-jest 27's 3-arg process() treats
# the 3rd positional arg as its options object and reads options.config.cwd -> undefined ->
# "Cannot read properties of undefined (reading 'cwd')" and every suite fails to run.
# Downgrading babel-jest is NOT an option (26 has no `.default` -> the destructure throws), so
# keep babel-jest 27 and patch the single forwarded call to wrap {...transformOptions, config}
# so babel-jest 27 sees options.config.cwd. Only the tools>=13 (`.default`) variant is touched;
# the tools 10.x codePreprocessor (used by the already-resolved PRs) does not match and is left
# untouched.
node << 'CPEOF' || true
const fs = require('fs');
const p = 'node_modules/@ant-design/tools/lib/jest/codePreprocessor.js';
try {
  let c = fs.readFileSync(p, 'utf8');
  const needsDefault = c.includes("require('babel-jest').default");
  const target = 'babelJest.process(src, fileName, config, transformOptions)';
  if (needsDefault && c.includes(target)) {
    c = c.replace(target, 'babelJest.process(src, fileName, Object.assign({}, transformOptions, { config }))');
    fs.writeFileSync(p, c);
    console.log('era_fixups: patched @ant-design/tools codePreprocessor process() config shape');
  } else {
    console.log('era_fixups: codePreprocessor patch not applicable (tools<13 or already patched)');
  }
} catch (e) { console.log('era_fixups: no @ant-design/tools codePreprocessor to patch'); }
CPEOF
"""

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(".", "prepare.sh", prepare_sh),
            File(".", "era_fixups.sh", era_fixups_sh),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{repo} || exit 1
{test_cmd}
exit 0
""".format(repo=repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{repo} || exit 1
git checkout -- . 2>/dev/null || true
# --3way: fall back to a blob-based merge when strict context matching fails (several PRs'
# package.json hunks don't match line-for-line but merge cleanly via the patch's index blobs).
git apply --3way --whitespace=nowarn /home/test.patch
# If the patch bumped a dependency in package.json, reinstall so the new version is present,
# then re-apply era fixups (npm install can re-drift babel-jest/cheerio).
if ! git diff --quiet -- package.json 2>/dev/null; then
  echo "test-run: package.json changed by patch; reinstalling deps"
  for a in 1 2 3; do npm install --legacy-peer-deps --no-audit --no-fund && break; echo "test-run: reinstall attempt $a failed; retry 15s"; sleep 15; done
  bash /home/era_fixups.sh || true
fi
{test_cmd}
exit 0
""".format(repo=repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

cd /home/{repo} || exit 1
git checkout -- . 2>/dev/null || true
# --3way: fall back to a blob-based merge when strict context matching fails. Without it, a
# fix.patch whose package.json hunk drifted (e.g. pr-26923's rc-notification bump) is silently
# rejected -> the dependency never bumps -> the fix appears not to work.
git apply --3way --whitespace=nowarn /home/test.patch /home/fix.patch
# The fix.patch may bump a dependency in package.json (e.g. rc-field-form ~1.10->~1.11).
# Reinstall so the fix runs against the version it expects, then re-apply era fixups.
if ! git diff --quiet -- package.json 2>/dev/null; then
  echo "fix-run: package.json changed by patch; reinstalling deps"
  for a in 1 2 3; do npm install --legacy-peer-deps --no-audit --no-fund && break; echo "fix-run: reinstall attempt $a failed; retry 15s"; sleep 15; done
  bash /home/era_fixups.sh || true
fi
{test_cmd}
exit 0
""".format(repo=repo, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening_block = f"""RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{self.global_env}

WORKDIR /home/{repo}

ENV NODE_ENV=test \\
    CI=true

RUN npm config set fetch-timeout 120000 2>/dev/null && \\
    npm config set fetch-retries 5 2>/dev/null && \\
    npm config set fetch-retry-mintimeout 10000 2>/dev/null && \\
    npm config set fetch-retry-maxtimeout 60000 2>/dev/null && \\
    npm config set maxsockets 3 2>/dev/null || true

{hardening_block}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


# Registered ONLY under the number_interval key so this shard does NOT clobber the generic
# ("ant-design","ant-design") config or the sibling shards (per team instruction).
@Instance.register("ant-design", "ant_design_29059_to_26481")
class AntDesign_29059_to_26481(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _AntDesignImageDefault(self.pr, self._config)

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
        return parse_jest_log(test_log)
