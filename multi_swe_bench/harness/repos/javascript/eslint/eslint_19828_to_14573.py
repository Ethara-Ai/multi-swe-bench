import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RANGE_TAG = re.split(r"[\\/]", __file__)[-1].removesuffix(".py").removeprefix("eslint_")
_BASE_TAG = f"base-{_RANGE_TAG}"
_BUILDER_IMAGE = "node:20-bookworm"
_MARKER = "===== test results ====="
_TIMEOUT = "120000"
_MOCHA_BIN = "node_modules/mocha/bin/_mocha"
_EXCLUDES = (
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' "
    "--exclude='*.gif' --exclude='*.ico' --exclude='*.woff' --exclude='*.woff2'"
)

_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BUILDER_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    FORCE_COLOR=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    NODE_ENV=test \\
    CI=true

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates curl git make python3 g++ pkg-config \\
    && update-ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && \\
    git config --global advice.detachedHead false

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""

_PIN_BLOCK = """RUN git reset --hard
RUN git checkout ${BASE_COMMIT}"""

_SCRUB_BLOCK = """RUN set -eux; \\
    git checkout --detach "${BASE_COMMIT}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
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

_PREPARE_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

export CI=true
export NODE_ENV=test

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

echo "CHECKOUT_SHA: $(git rev-parse HEAD)"

ERA_CUTOFF="$(git show -s --format=%cI HEAD)"
echo "ERA_CUTOFF: ${ERA_CUTOFF}"

if npm install --ignore-scripts --no-audit --no-fund --before="${ERA_CUTOFF}"; then
    echo "NPM_INSTALL: era-pinned"
elif npm install --ignore-scripts --no-audit --no-fund; then
    echo "NPM_INSTALL: range-resolved"
else
    echo "NPM_INSTALL: peer conflict, retrying with legacy peer deps"
    npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps
fi

test -f __MOCHA_BIN__
node __MOCHA_BIN__ --version
node -e "require.resolve('mocha');require.resolve('chai');require.resolve('sinon')"
test -d tests/lib

echo "DEPS_OK: __MOCHA_BIN__"
"""

_EMIT_JS = (
    r"var fs=require('fs'),path=require('path'),d;"
    r"try{d=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));}"
    r"catch(e){process.stdout.write('TESTCASE FAILED mocha-report-unreadable\n');process.exit(0);}"
    r"var seen={},out=[];"
    r"function add(s,t){var n=String((t&&t.fullTitle)||'').replace(/\s+/g,' ').trim();"
    r"if(!n)return;"
    r"var f=(t&&t.file)?path.relative(process.cwd(),String(t.file)):'';"
    r"if(f)n=n+' ('+f+')';"
    r"if(seen[n])return;seen[n]=1;out.push('TESTCASE '+s+' '+n);}"
    r"(d.failures||[]).forEach(function(t){add('FAILED',t);});"
    r"(d.pending||[]).forEach(function(t){add('SKIPPED',t);});"
    r"(d.passes||[]).forEach(function(t){add('PASSED',t);});"
    r"process.stdout.write(out.join('\n')+'\n');"
)

_DEPS_SYNC = """if ! npm ls --depth=0 >/dev/null 2>&1; then
    echo "DEPS_DRIFT: installed tree does not satisfy package.json, re-syncing"
    if npm install --ignore-scripts --no-audit --no-fund; then
        echo "DEPS_RESYNC: range-resolved"
    elif npm install --ignore-scripts --no-audit --no-fund --legacy-peer-deps; then
        echo "DEPS_RESYNC: legacy-peer-deps"
    else
        echo "DEPS_RESYNC: failed, continuing with existing tree"
    fi
fi
"""

_TEST_BLOCK = _DEPS_SYNC + """
TARGETS=(
__TARGETS__
)

EXISTING=()
for f in "${TARGETS[@]:-}"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
        EXISTING+=("$f")
    fi
done

echo "===== mocha targets: ${#EXISTING[@]} of ${#TARGETS[@]} present ====="

REPORT=/home/mocha-report.json
LOG=/home/mocha-stderr.log
MERGED=/home/mocha-merged.txt
rm -f "$REPORT" "$LOG" "$MERGED"

if [ "${#EXISTING[@]}" -eq 0 ]; then
    echo "__MARKER__"
    echo "no target test file present at this stage"
    exit 0
fi

set +e
node __MOCHA_BIN__ --reporter json --timeout __TIMEOUT__ "${EXISTING[@]}" > "$REPORT" 2> "$LOG"
MOCHA_EXIT=$?
set -e

echo "===== mocha exit code: ${MOCHA_EXIT} ====="
tail -c 200000 "$LOG" || true

if [ -s "$REPORT" ]; then
    echo "__MARKER__"
    node -e "__EMIT_JS__" "$REPORT"
    exit 0
fi

echo "===== no batch report: isolating ${#EXISTING[@]} file(s) ====="
: > "$MERGED"
for f in "${EXISTING[@]}"; do
    rm -f "$REPORT" "$LOG"
    set +e
    node __MOCHA_BIN__ --reporter json --timeout __TIMEOUT__ "$f" > "$REPORT" 2> "$LOG"
    set -e
    if [ -s "$REPORT" ]; then
        node -e "__EMIT_JS__" "$REPORT" >> "$MERGED"
    else
        echo "TESTCASE FAILED mocha-load-error ($f)" >> "$MERGED"
        echo "----- load failure: $f -----"
        tail -c 4000 "$LOG" || true
    fi
done
echo "__MARKER__"
cat "$MERGED\""""

_RUN_HEAD = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/__REPO__
"""

_RUN_TESTS_SH = _RUN_HEAD + """
__TEST_BLOCK__
"""

_RUN_SH = _RUN_HEAD + """
bash /home/run_tests.sh
"""

_TEST_RUN_SH = _RUN_HEAD + """
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch \
    || git apply -3 --whitespace=nowarn __EXCLUDES__ /home/test.patch

bash /home/run_tests.sh
"""

_FIX_RUN_SH = _RUN_HEAD + """
git apply --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \
    || git apply -3 --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch

bash /home/run_tests.sh
"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")
_DIFF_FILE_RE = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.MULTILINE)
_TESTCASE_FILE_RE = re.compile(r"\s\(([^()]+\.(?:c|m)?[jt]sx?)\)$")

_SUITE_ROOTS = ("tests/bin/", "tests/conf/", "tests/lib/", "tests/messages/", "tests/tools/")
_NON_SUITE_SEGMENTS = ("fixtures", "bench", "node_modules")


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


def _targets_block(pr: PullRequest) -> str:
    targets = _test_targets(pr)
    if not targets:
        return "    ''"
    return "\n".join("    '" + t.replace("'", "'\\''") + "'" for t in targets)


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("__TEST_BLOCK__", _TEST_BLOCK)
        .replace("__EMIT_JS__", _EMIT_JS)
        .replace("__TARGETS__", _targets_block(pr))
        .replace("__MOCHA_BIN__", _MOCHA_BIN)
        .replace("__TIMEOUT__", _TIMEOUT)
        .replace("__EXCLUDES__", _EXCLUDES)
        .replace("__MARKER__", _MARKER)
        .replace("__BUILDER_IMAGE__", _BUILDER_IMAGE)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__ORG__", pr.org)
        .replace("__REPO__", pr.repo)
    )


class EslintImageBase_19828_TO_14573(Image):
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
        return _render(_BASE_DOCKERFILE, self.pr)


class EslintImageDefault_19828_TO_14573(Image):
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
        return EslintImageBase_19828_TO_14573(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", _render(_CHECK_GIT_CHANGES_SH, self.pr)),
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, self.pr)),
            File(".", "test-run.sh", _render(_TEST_RUN_SH, self.pr)),
            File(".", "fix-run.sh", _render(_FIX_RUN_SH, self.pr)),
        ]

    def dockerfile(self) -> str:
        copies = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        sections = [
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\nENV BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"WORKDIR /home/{self.pr.repo}",
            _PIN_BLOCK,
            copies,
            "RUN bash /home/prepare.sh",
            _render(_SCRUB_BLOCK, self.pr),
            self.clear_env,
        ]
        return "\n\n".join(part.strip() for part in sections if part.strip()) + "\n"


@Instance.register("eslint", f"eslint_{_RANGE_TAG}")
class ESLINT_19828_TO_14573(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return EslintImageDefault_19828_TO_14573(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        clean = _ANSI_RE.sub("", log)
        body = clean.rsplit(_MARKER, 1)[1] if _MARKER in clean else ""

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
            target = _TESTCASE_FILE_RE.search(name)
            if target:
                name = f"{target.group(1)} > {name[: target.start()].strip()}"
            if status == "PASSED":
                passed.add(name)
            elif status == "FAILED":
                failed.add(name)
            else:
                skipped.add(name)

        passed -= failed
        skipped -= failed | passed
        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
