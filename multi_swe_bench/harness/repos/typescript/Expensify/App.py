from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:20.19.5"

_PR_NUMBERS: set = {68241, 68748, 68929, 70615, 70617, 71233, 71323, 72267, 72448, 72847}

TEST_CMD = (
    "TZ=utc node_modules/.bin/jest --ci --silent --json "
    "--maxWorkers=4 --workerIdleMemoryLimit=1200MB "
    "--outputFile=/tmp/jest-results.json; "
    "node /home/jest-report.js /tmp/jest-results.json"
)


_JEST_REPORT_JS = """const fs = require('fs');

const path = process.argv[2];
if (!path || !fs.existsSync(path)) {
    console.log('jest-report: no results file at ' + path + ' -- jest produced no output');
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('jest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED'};

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\\\/g, '/');
    const marker = '/__PR_REPO__/';
    const at = file.indexOf(marker);
    if (at !== -1) file = file.slice(at + marker.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of results) {
        const parts = [file].concat(a.ancestorTitles || [], [a.title]);
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + parts.join(' > '));
    }
}

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
"""


_APPLY_PATCH_SH = r"""#!/bin/bash
patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

exit 0
"""


class ExpensifyAppImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        nums = _PR_NUMBERS or {self.pr.number}
        return f"base-{min(nums)}-{max(nums)}"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        pkg-config python3 make g++ \\
        libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{fetch}

CMD ["/bin/bash"]
"""


class ExpensifyAppImageDefault(Image):
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
        return ExpensifyAppImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        jest_report_js = _JEST_REPORT_JS.replace("__PR_REPO__", self.pr.repo)
        apply_patch_sh = _APPLY_PATCH_SH

        prepare_sh = f"""#!/bin/bash
set -e

cd /home/{self.pr.repo}

cat > /home/jest-report.js <<'__JEST_REPORT_EOF__'
{jest_report_js}__JEST_REPORT_EOF__

cat > /home/apply_patch.sh <<'__APPLY_PATCH_EOF__'
{apply_patch_sh}__APPLY_PATCH_EOF__
chmod +x /home/apply_patch.sh

bash /home/check_git_changes.sh
BASE_COMMIT="${{BASE_COMMIT:-{self.pr.base.sha}}}"
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"

ok=0
for attempt in 1 2 3 4; do
    if [ "$attempt" -eq 1 ]; then
        extra=""
    else
        extra="--prefer-offline"
        echo "prepare: npm ci attempt $attempt (retrying with cache)"
    fi
    if npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund $extra; then
        ok=1
        echo "prepare: npm ci succeeded on attempt $attempt"
        break
    fi
    echo "prepare: npm ci attempt $attempt failed; retrying in 15s"
    sleep 15
done
if [ "$ok" -ne 1 ]; then
    echo "prepare: npm ci FAILED after 4 attempts"
    exit 1
fi

npm rebuild canvas 2>&1 || npm install --no-save --no-package-lock canvas 2>&1 \
    || echo "WARN: canvas rebuild failed, jsdom-based tests will fail to load"

if [ -d patches ]; then
    rm -rf ./msb-patches
    mkdir -p ./msb-patches
    find ./patches -type f -name '*.patch' -exec cp {{}} ./msb-patches ';'
    n_patches=$(ls ./msb-patches 2>/dev/null | wc -l | tr -d ' ')
    echo "prepare: applying $n_patches patch-package patches (flattened from nested patches/)"
    if ! npx --no-install patch-package --patch-dir ./msb-patches; then
        echo "WARN: one or more patch-package patches failed to apply; continuing"
    fi
    rm -rf ./msb-patches
else
    echo "prepare: no patches/ directory found; skipping patch-package"
fi

git checkout -- package.json package-lock.json
git clean -fdq
bash /home/check_git_changes.sh

node --version
npm --version
node_modules/.bin/jest --version
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
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail
export CI=true

cd /home/{pr.repo} || exit 1
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=TEST_CMD),
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
    CI=true \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    npm_config_engine_strict=false \\
    NODE_OPTIONS=--experimental-vm-modules

RUN set -eux; \\
    for t in git curl patch csplit node npm; do \\
        command -v "$t" >/dev/null 2>&1 || {{ echo "missing required tool: $t"; exit 1; }}; \\
    done; \\
    node --version; \\
    npm --version

RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\
    git config --global url."https://github.com/".insteadOf "git@github.com:"

RUN npm config set fetch-timeout 900000 && \\
    npm config set fetch-retries 8 && \\
    npm config set fetch-retry-mintimeout 20000 && \\
    npm config set fetch-retry-maxtimeout 180000 && \\
    npm config set maxsockets 6

{hardening_block}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("Expensify", "App")
class ExpensifyApp(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _PR_NUMBERS.add(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ExpensifyAppImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S.*)$")

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip()
            if not name:
                continue
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
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


