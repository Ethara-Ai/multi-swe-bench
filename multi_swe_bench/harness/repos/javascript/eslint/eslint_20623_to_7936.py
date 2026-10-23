import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RESULTS_MARKER = "===== test results ====="
_MOCHA_TIMEOUT = "60000"
_BASE_WALK_LIMIT = "50"
_BUILDER_IMAGE = "node:22-bookworm"
_RANGE_TAG = (
    re.split(r"[\\/]", __file__)[-1].removesuffix(".py").removeprefix("eslint_")
)
_BASE_TAG = f"base-{_RANGE_TAG}"
_NODE_PREFIX = "/opt/node"
_NPM_PREFIX = "/opt/npm-compat"
_NPM_FALLBACK = "6.14.18"
_NODE_CANDIDATES = (
    "6.17.1",
    "8.17.0",
    "10.24.1",
    "12.22.12",
    "14.21.3",
    "16.20.2",
    "18.20.8",
    "20.19.0",
    "22.14.0",
)

_INFRA_ARGS = """ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt\""""

_INFRA_ENV = """ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}"""

_CERT_SYMLINKS = """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"""

_LABEL_BLOCK = """LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/\""""

_APT_BLOCK = """RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends \\
        ca-certificates \\
        curl \\
        xz-utils \\
        git \\
        make \\
        python3 \\
        build-essential \\
        pkg-config; \\
    update-ca-certificates; \\
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    git config --global --add safe.directory '*'; \\
    git config --global advice.detachedHead false"""

_PIN_BLOCK = """RUN git cat-file -e "${BASE_COMMIT}^{commit}" || git fetch --no-tags origin ${BASE_COMMIT}

RUN git reset --hard
RUN git checkout ${BASE_COMMIT}"""

_SCRUB_BLOCK = """RUN set -eux; \\
    BASE_SHA="$(cat /home/.base_sha)"; \\
    test -n "${BASE_SHA}"; \\
    git checkout --detach "${BASE_SHA}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_SHA}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__
if [ -n "$(git status --porcelain)" ]; then
    echo "WORKTREE_DIRTY"
    git status --porcelain
    exit 1
fi
echo "WORKTREE_CLEAN"
"""

_RESOLVE_BASE_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

DECLARED="__BASE_SHA__"
PROBE=/home/.base_probe

awk '
/^diff --git a\\// { p = $0; sub(/^diff --git a\\//, "", p); sub(/ b\\/.*$/, "", p); next }
/^index [0-9a-f]+\\.\\.[0-9a-f]+/ {
    if (p != "") {
        split($2, h, /\\.\\./)
        if (h[1] !~ /^0+$/) { print p "\\t" h[1] }
        p = ""
    }
}
' /home/test.patch /home/fix.patch | sort -u > "${PROBE}"

score_sha() {
    local sha="$1"
    cut -f1 "${PROBE}" | sed "s|^|${sha}:|" | git cat-file --batch-check | awk -v probe="${PROBE}" '
        BEGIN { n = 0; while ((getline line < probe) > 0) { split(line, a, "\\t"); n++; want[n] = a[2] } i = 0 }
        { i++; if ($2 == "blob") { if (index($1, want[i]) == 1) { ok++ } else { bad++ } } }
        END { printf "%d %d\\n", ok + 0, bad + 0 }'
}

RESOLVED=""
if [ -s "${PROBE}" ]; then
    for sha in $(git rev-list --first-parent -n __BASE_WALK_LIMIT__ "${DECLARED}"); do
        read -r OK BAD <<< "$(score_sha "${sha}")"
        if [ "${OK}" -gt 0 ] && [ "${BAD}" -eq 0 ]; then
            RESOLVED="${sha}"
            break
        fi
    done
fi

if [ -z "${RESOLVED}" ]; then
    RESOLVED="${DECLARED}"
    echo "BASE_SHA_UNRESOLVED: keeping ${DECLARED}"
elif [ "${RESOLVED}" = "${DECLARED}" ]; then
    echo "BASE_SHA_OK: ${RESOLVED}"
else
    echo "BASE_SHA_RESOLVED: ${DECLARED} -> ${RESOLVED}"
fi

rm -f "${PROBE}"
printf '%s\\n' "${RESOLVED}" > /home/.base_sha

git checkout --detach "${RESOLVED}"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
"""

_RESOLVE_JS = (
    "var fs=require('fs');"
    "var p=process.argv[1];var cands=String(process.argv[2]).split(',');"
    "var eng='';try{var pkg=JSON.parse(fs.readFileSync(p,'utf8'));"
    "eng=(pkg.engines&&pkg.engines.node)?String(pkg.engines.node):'';}catch(e){eng='';}"
    "function pv(v){var m=String(v).trim().replace(/^[v=]+/,'').split('-')[0].split('.');"
    "return [parseInt(m[0],10)||0,parseInt(m[1],10)||0,parseInt(m[2],10)||0];}"
    "function cmp(a,b){for(var i=0;i<3;i++){if(a[i]!==b[i])return a[i]<b[i]?-1:1;}return 0;}"
    "function one(v,c){c=String(c).trim();if(!c||c==='*'||c==='x')return true;"
    "var m=c.match(/^(>=|<=|>|<|=|\\^|~)?\\s*v?(\\d+)(?:\\.(\\d+|x|\\*))?(?:\\.(\\d+|x|\\*))?/);"
    "if(!m)return true;"
    "var op=m[1]||'=';var M=parseInt(m[2],10);"
    "var mi=(m[3]===undefined||m[3]==='x'||m[3]==='*')?null:parseInt(m[3],10);"
    "var pa=(m[4]===undefined||m[4]==='x'||m[4]==='*')?null:parseInt(m[4],10);"
    "var lo=[M,mi||0,pa||0];var r=cmp(v,lo);"
    "var part=(mi===null||pa===null);"
    "var pb=(mi===null)?[M+1,0,0]:[M,mi+1,0];"
    "if(op==='>=')return r>=0;"
    "if(op==='>')return part?cmp(v,pb)>=0:r>0;"
    "if(op==='<=')return part?cmp(v,pb)<0:r<=0;"
    "if(op==='<')return r<0;"
    "if(op==='^'){var hi;if(M>0){hi=[M+1,0,0];}"
    "else if(mi!==null&&mi>0){hi=[0,mi+1,0];}"
    "else if(mi!==null&&pa!==null){hi=[0,mi,pa+1];}"
    "else if(mi!==null){hi=[0,mi+1,0];}else{hi=[1,0,0];}"
    "return r>=0&&cmp(v,hi)<0;}"
    "if(op==='~'){var h2=(mi!==null)?[M,mi+1,0]:[M+1,0,0];return r>=0&&cmp(v,h2)<0;}"
    "if(mi===null)return v[0]===M;"
    "if(pa===null)return v[0]===M&&v[1]===mi;"
    "return cmp(v,lo)===0;}"
    "function sat(v,range){if(!range||!range.trim())return true;"
    "var ors=String(range).split('||');"
    "for(var i=0;i<ors.length;i++){var parts=ors[i].trim().split(/\\s+/);var ok=true;"
    "for(var j=0;j<parts.length;j++){if(!one(v,parts[j])){ok=false;break;}}"
    "if(ok)return true;}return false;}"
    "var pick='';"
    "for(var i=0;i<cands.length;i++){if(sat(pv(cands[i]),eng)){pick=cands[i];break;}}"
    "if(!pick)pick=cands[cands.length-1];"
    "process.stdout.write(pick);"
)

_EMIT_JS = (
    "var fs=require('fs');var p=process.argv[1];var d;"
    "try{d=JSON.parse(fs.readFileSync(p,'utf8'));}"
    "catch(e){process.stdout.write('TESTCASE FAILED mocha-json-unreadable\\n');process.exit(0);}"
    "var out=[];var seen={};var path=require('path');"
    "function add(s,t){var n=String((t&&t.fullTitle)||'').replace(/\\s+/g,' ').trim();"
    "if(!n)return;"
    "var f=(t&&t.file)?path.relative(process.cwd(),String(t.file)):'';"
    "if(f)n=n+' ('+f+')';"
    "if(seen[n])return;seen[n]=1;out.push('TESTCASE '+s+' '+n);}"
    "(d.failures||[]).forEach(function(t){add('FAILED',t);});"
    "(d.pending||[]).forEach(function(t){add('SKIPPED',t);});"
    "(d.passes||[]).forEach(function(t){add('PASSED',t);});"
    "process.stdout.write(out.join('\\n')+'\\n');"
)

_GIT_APPLY_EXCLUDES = (
    "--exclude='*.png' --exclude='*.ico' --exclude='*.ttf' "
    "--exclude='*.woff' --exclude='*.woff2' --exclude='*.jpg' "
    "--exclude='*.jpeg' --exclude='*.gif'"
)

_PREPARE_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

BASE_SHA="$(cat /home/.base_sha 2>/dev/null || true)"
if [ -z "${BASE_SHA}" ]; then
    BASE_SHA="__BASE_SHA__"
fi

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach "${BASE_SHA}"
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

echo "CHECKOUT_SHA: ${BASE_SHA}"

export CI=true
export NODE_ENV=test

TARGET_VERSION="$(node -e "__RESOLVE_JS__" /home/__REPO__/package.json '__NODE_CANDIDATES__')"
test -n "${TARGET_VERSION}"
echo "NODE_TARGET: ${TARGET_VERSION}"

case "$(uname -m)" in
    aarch64|arm64) NODE_ARCH=arm64 ;;
    x86_64|amd64) NODE_ARCH=x64 ;;
    *) echo "UNSUPPORTED_ARCH: $(uname -m)"; exit 1 ;;
esac

mkdir -p __NODE_PREFIX__
curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${TARGET_VERSION}/node-v${TARGET_VERSION}-linux-${NODE_ARCH}.tar.xz"
tar -xJf /tmp/node.tar.xz -C __NODE_PREFIX__ --strip-components=1
rm -f /tmp/node.tar.xz

NODE_BIN=__NODE_PREFIX__/bin/node
NPM_CLI=__NODE_PREFIX__/lib/node_modules/npm/bin/npm-cli.js
"$NODE_BIN" --version

NPM_MAJOR="$("$NODE_BIN" "$NPM_CLI" --version | cut -d. -f1)"
if [ "${NPM_MAJOR}" -lt 5 ]; then
    mkdir -p __NPM_PREFIX__
    curl -fsSL -o /tmp/npm.tgz "https://registry.npmjs.org/npm/-/npm-__NPM_FALLBACK__.tgz"
    tar -xzf /tmp/npm.tgz -C __NPM_PREFIX__ --strip-components=1
    rm -f /tmp/npm.tgz
    NPM_CLI=__NPM_PREFIX__/bin/npm-cli.js
fi
echo "NPM_VERSION: $("$NODE_BIN" "$NPM_CLI" --version)"
printf '%s\\n' "$NPM_CLI" > /home/.npm_cli

"$NODE_BIN" "$NPM_CLI" install --ignore-scripts --no-audit --no-fund \\
    || "$NODE_BIN" "$NPM_CLI" install --ignore-scripts --no-audit --no-fund --no-optional

if [ -f node_modules/.bin/_mocha ]; then
    MOCHA_BIN=node_modules/.bin/_mocha
elif [ -f node_modules/mocha/bin/_mocha ]; then
    MOCHA_BIN=node_modules/mocha/bin/_mocha
else
    echo "DEPS_FAILED: mocha runner missing after npm install"
    exit 1
fi
"$NODE_BIN" "$MOCHA_BIN" --version

SMOKE_TARGETS=(
__JEST_TARGETS__
)

SMOKE_EXISTING=()
for f in "${SMOKE_TARGETS[@]:-}"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
        SMOKE_EXISTING+=("$f")
    fi
    if [ "${#SMOKE_EXISTING[@]}" -ge 3 ]; then
        break
    fi
done

if [ "${#SMOKE_EXISTING[@]}" -eq 0 ]; then
    echo "SMOKE_SKIPPED: no target suite exists at base commit"
else
    set +e
    "$NODE_BIN" "$MOCHA_BIN" --reporter json --timeout __MOCHA_TIMEOUT__ \\
        "${SMOKE_EXISTING[@]}" > /tmp/smoke.json 2> /tmp/smoke.log
    set -e
    tail -c 20000 /tmp/smoke.log || true
    test -s /tmp/smoke.json
    "$NODE_BIN" -e '
const fs = require("fs");
let d;
try { d = JSON.parse(fs.readFileSync(process.argv[1], "utf8")); }
catch (e) { console.log("DEPS_FAILED: mocha report unparseable"); process.exit(1); }
const n = (d.stats && d.stats.tests) || 0;
if (n < 1) { console.log("DEPS_FAILED: mocha collected 0 test cases"); process.exit(1); }
console.log("SMOKE_OK: " + n + " test cases collected");
' /tmp/smoke.json
    rm -f /tmp/smoke.json /tmp/smoke.log
fi

echo "DEPS_OK: ${MOCHA_BIN}"
"""

_TEST_BLOCK = """TARGETS=(
__JEST_TARGETS__
)

EXISTING=()
for f in "${TARGETS[@]:-}"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
        EXISTING+=("$f")
    fi
done

echo "===== mocha scope: ${#EXISTING[@]} of ${#TARGETS[@]} target files present ====="

if [ "${#EXISTING[@]}" -eq 0 ]; then
    echo "__RESULTS_MARKER__"
    echo "no target test file exists at this stage"
    exit 0
fi

if [ -f node_modules/.bin/_mocha ]; then
    MOCHA_BIN=node_modules/.bin/_mocha
elif [ -f node_modules/mocha/bin/_mocha ]; then
    MOCHA_BIN=node_modules/mocha/bin/_mocha
else
    echo "__RESULTS_MARKER__"
    echo "TESTCASE FAILED mocha-runner-missing"
    exit 0
fi

MOCHA_JSON=/home/mocha-report.json
MOCHA_LOG=/home/mocha-stderr.log
MOCHA_ONE=/home/mocha-one.json
MOCHA_ONE_LOG=/home/mocha-one.log
MOCHA_MERGED=/home/mocha-merged.txt
rm -f "$MOCHA_JSON" "$MOCHA_LOG" "$MOCHA_ONE" "$MOCHA_ONE_LOG" "$MOCHA_MERGED"

set +e
node "$MOCHA_BIN" --reporter json --timeout __MOCHA_TIMEOUT__ "${EXISTING[@]}" > "$MOCHA_JSON" 2> "$MOCHA_LOG"
MOCHA_EXIT=$?
set -e

echo "===== mocha exit code: ${MOCHA_EXIT} ====="
tail -c 200000 "$MOCHA_LOG" || true

if [ -s "$MOCHA_JSON" ]; then
    echo "__RESULTS_MARKER__"
    node -e "__EMIT_JS__" "$MOCHA_JSON"
    exit 0
fi

echo "===== batch produced no report: isolating ${#EXISTING[@]} file(s) ====="
: > "$MOCHA_MERGED"
for f in "${EXISTING[@]}"; do
    rm -f "$MOCHA_ONE" "$MOCHA_ONE_LOG"
    set +e
    node "$MOCHA_BIN" --reporter json --timeout __MOCHA_TIMEOUT__ "$f" > "$MOCHA_ONE" 2> "$MOCHA_ONE_LOG"
    set -e
    if [ -s "$MOCHA_ONE" ]; then
        node -e "__EMIT_JS__" "$MOCHA_ONE" >> "$MOCHA_MERGED"
    else
        echo "TESTCASE FAILED mocha-load-error (${f})" >> "$MOCHA_MERGED"
        echo "----- load failure: ${f} -----"
        tail -c 2000 "$MOCHA_ONE_LOG" || true
    fi
done
echo "__RESULTS_MARKER__"
cat "$MOCHA_MERGED\""""

_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="__NODE_PREFIX__/bin:$PATH"

cd /home/__REPO__

__TEST_BLOCK__
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="__NODE_PREFIX__/bin:$PATH"

cd /home/__REPO__

git apply --whitespace=nowarn __GIT_APPLY_EXCLUDES__ /home/test.patch

__TEST_BLOCK__
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export PATH="__NODE_PREFIX__/bin:$PATH"

cd /home/__REPO__

git apply --whitespace=nowarn __GIT_APPLY_EXCLUDES__ /home/test.patch /home/fix.patch

__TEST_BLOCK__
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.MULTILINE)


_SUITE_ROOTS = (
    "tests/bin/",
    "tests/conf/",
    "tests/lib/",
    "tests/messages/",
    "tests/tools/",
)
_NON_SUITE_SEGMENTS = ("fixtures", "bench")


def _test_targets(pr: PullRequest) -> list[str]:
    targets: set[str] = set()
    for _, new_path in _DIFF_FILE_RE.findall(pr.test_patch or ""):
        path = new_path.strip()
        if not path.startswith(_SUITE_ROOTS) or not path.endswith(".js"):
            continue
        segments = path.split("/")
        if any(s in _NON_SUITE_SEGMENTS for s in segments):
            continue
        if any(s.startswith("_") for s in segments[:-1]):
            continue
        targets.add(path)
    return sorted(targets)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _targets_block(pr: PullRequest) -> str:
    targets = _test_targets(pr)
    if not targets:
        return "    ''"
    return "\n".join(f"    {_shell_quote(t)}" for t in targets)


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__TEST_BLOCK__", _TEST_BLOCK)
        .replace("__EMIT_JS__", _EMIT_JS)
        .replace("__RESOLVE_JS__", _RESOLVE_JS)
        .replace("__NODE_CANDIDATES__", ",".join(_NODE_CANDIDATES))
        .replace("__NODE_PREFIX__", _NODE_PREFIX)
        .replace("__NPM_PREFIX__", _NPM_PREFIX)
        .replace("__NPM_FALLBACK__", _NPM_FALLBACK)
        .replace("__JEST_TARGETS__", _targets_block(pr))
        .replace("__MOCHA_TIMEOUT__", _MOCHA_TIMEOUT)
        .replace("__BASE_WALK_LIMIT__", _BASE_WALK_LIMIT)
        .replace("__GIT_APPLY_EXCLUDES__", _GIT_APPLY_EXCLUDES)
        .replace("__RESULTS_MARKER__", _RESULTS_MARKER)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
    )


def _disjoint(
    passed: set[str], failed: set[str], skipped: set[str]
) -> TestResult:
    passed = passed - failed
    skipped = skipped - failed - passed
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


class EslintImageBase_20623_TO_7936(Image):
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
        return _BUILDER_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if self.config.need_clone:
            code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{_render(_INFRA_ARGS, self.pr)}

{_INFRA_ENV}

{_render(_LABEL_BLOCK, self.pr)}

{_CERT_SYMLINKS}

{_APT_BLOCK}

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class EslintImageDefault_20623_TO_7936(Image):
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
        return EslintImageBase_20623_TO_7936(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def prepare_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _render(_CHECK_GIT_CHANGES_SH, self.pr)),
            File(".", "resolve_base.sh", _render(_RESOLVE_BASE_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
        ]

    def graded_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def files(self) -> list[File]:
        return self.prepare_files() + self.graded_files()

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        sections = [
            f"FROM {base.image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n'
            f'ENV BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            _PIN_BLOCK,
            copies,
            "RUN bash /home/resolve_base.sh",
            _SCRUB_BLOCK,
            "RUN bash /home/prepare.sh",
            self.clear_env,
        ]
        return "\n\n".join(part.strip() for part in sections if part.strip()) + "\n"


@Instance.register("eslint", "eslint")
@Instance.register("eslint", f"eslint_{_RANGE_TAG}")
class ESLINT_20623_TO_7936(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return EslintImageDefault_20623_TO_7936(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        clean = _ANSI_RE.sub("", log)
        if _RESULTS_MARKER in clean:
            body = clean.rsplit(_RESULTS_MARKER, 1)[1]
        else:
            body = ""

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        for line in body.splitlines():
            match = _TESTCASE_RE.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed.add(name)
            elif status == "FAILED":
                failed.add(name)
            else:
                skipped.add(name)

        return _disjoint(passed, failed, skipped)
