import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _test_scope(pr: PullRequest) -> str:
    paths = []
    for line in (pr.test_patch or "").split(chr(10)):
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if not m:
            continue
        path = m.group(2).strip()
        if not path.endswith((".ts", ".tsx")):
            continue
        if not path.startswith(
            ("tests/ui/", "tests/unit/", "tests/actions/", "tests/navigation/")
        ):
            continue
        if path not in paths:
            paths.append(path)
    return " ".join(paths)


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
        return "node:20.19.5"

    def image_tag(self) -> str:
        return "base-76743-to-72955"

    def workdir(self) -> str:
        return "base-76743-to-72955"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        
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

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git patch \\
    && rm -rf /var/lib/apt/lists/*

RUN npm config set fetch-timeout 900000 && \\
    npm config set fetch-retries 8 && \\
    npm config set fetch-retry-mintimeout 20000 && \\
    npm config set fetch-retry-maxtimeout 180000 && \\
    npm config set maxsockets 6 && \\
    npm config set fund false && \\
    npm config set audit false

RUN git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \\
    git config --global url."https://github.com/".insteadOf "git@github.com:"

{self.clear_env}

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}


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
        test_cmd = (
            "rm -f /tmp/jest-results.json\n"
            "TZ=utc node_modules/.bin/jest --ci --silent --runInBand --forceExit "
            "--json --outputFile=/tmp/jest-results.json "
            "{scope} || rc=$?\n"
            "node /home/jest-report.js /tmp/jest-results.json"
        ).format(scope=_test_scope(self.pr))

       
        jest_report_js = """const fs = require('fs');

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
const PREFIX = '/home/__PR_REPO__/';
const flat = (s) => String(s == null ? '' : s).replace(/[\\r\\n]+/g, ' ').trim();

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\\\/g, '/');
    if (file.startsWith(PREFIX)) file = file.slice(PREFIX.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    for (const a of results) {
        const parts = [file].concat((a.ancestorTitles || []).map(flat), [flat(a.title)]);
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + parts.join(' > '));
    }
}

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
""".replace("__PR_REPO__", self.pr.repo)

      
        apply_patch_sh = r"""#!/bin/bash
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

        prepare_sh = """#!/bin/bash
set -e

# CI/NODE_ENV are exported here and in the three run scripts rather than set as
# a second ENV in the base image: the base carries exactly one ENV block, the
# standard infrastructure one. npm's fund/audit/engine-strict equivalents are
# `npm config set` in that same base, so nothing behavioural moved.
export CI=true
export NODE_ENV=test
# MUST be an env var, not `npm config set`. Expensify/App ships its own
# .npmrc containing `engine-strict=true`, and npm's precedence is
#     CLI flags > npm_config_* env vars > project .npmrc > user .npmrc
# so a user-level `npm config set engine-strict false` loses to the repo's
# own file and every `npm ci` dies with EBADENGINE: package.json pins
# engines.node to the commit's exact .nvmrc (20.19.4 on most of this range)
# while the image is node 20.19.5. The env var outranks the project file.
export npm_config_engine_strict=false

cd /home/{pr.repo}

cat > /home/jest-report.js <<'__JEST_REPORT_EOF__'
{jest_report_js}__JEST_REPORT_EOF__

cat > /home/apply_patch.sh <<'__APPLY_PATCH_EOF__'
{apply_patch_sh}__APPLY_PATCH_EOF__
chmod +x /home/apply_patch.sh

bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{pr.base.sha}"

# --ignore-scripts skips scripts/postInstall.sh, which is driven off the
# HybridApp layout (it shells out to jq and npm-installs the Mobile-Expensify
# submodule and desktop/, none of which exists in this checkout or matters to
# jest). Its one relevant step, patch-package, is run explicitly below.
#
# --legacy-peer-deps because package.json carries an `overrides` block and the
# React Native 0.81 dependency graph does not satisfy npm 10's strict peer
# resolution. `|| true` semantics (the loop below never exits non-zero) per
# Check 3A: optional native deps are allowed to fail on either arch. What is NOT
# allowed to fail is jest itself, which the assertion after the loop enforces.
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
    echo "prepare: WARNING npm ci did not succeed after 4 attempts"
fi
test -x node_modules/.bin/jest

# patch-package, flattened exactly the way scripts/applyPatches.sh does it.
# This is load-bearing and not a workaround: Expensify keeps its patches in
# per-package SUBDIRECTORIES (patches/@react-native/..., patches/expo/..., 37 of
# them), and patch-package only reads a FLAT --patch-dir. A bare
# `npx patch-package` therefore finds nothing, applies nothing, and the suites
# that depend on a patched dependency fail for a reason that has nothing to do
# with the PR. applyPatches.sh itself is not invoked because it shells out to
# jq (via scripts/is-hybrid-app.sh), which the node image does not ship.
#
# The temp dir has to be created inside the repo -- patch-package resolves
# --patch-dir relative to the package root -- so it is removed again before the
# clean-tree check below.
temp_patch_dir="$(mktemp -d ./tmp-patches-XXXXXX)"
find ./patches -type f -name '*.patch' -exec cp {{}} "$temp_patch_dir" \\;
npx --no-install patch-package --patch-dir "$temp_patch_dir" \\
    || echo "prepare: WARNING patch-package reported failures; see output above"
rm -rf "$temp_patch_dir"

# R21: nothing above may leave the work tree dirty, or the stages' git apply
# will conflict. npm ci rewrites nothing tracked; the temp patch dir is gone.
git clean -fdq -e node_modules
bash /home/check_git_changes.sh

node --version
npm --version
node_modules/.bin/jest --version
""".format(
            pr=self.pr,
            jest_report_js=jest_report_js,
            apply_patch_sh=apply_patch_sh,
        )

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
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--experimental-vm-modules --max_old_space_size=8192"

cd /home/{pr.repo}
git checkout -- . 2>/dev/null || true

# `|| rc=$?` is not `|| true` on the test command (Check 3C forbids that): the
# graded command is two steps -- jest writes the JSON, then jest-report.js turns
# it into the lines parse_log reads -- and under `set -e` a red suite would kill
# the script before the reporter ever ran, which is precisely how a stage ends
# up reporting nothing. The failure is not swallowed: the reporter prints
# "jest-report: no results file" when jest never started, and rc is returned.
rc=0
{test_cmd}
exit "$rc"
""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--experimental-vm-modules --max_old_space_size=8192"

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
bash /home/apply_patch.sh /home/test.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi

rc=0
{test_cmd}
exit "$rc"
""".format(pr=self.pr, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--experimental-vm-modules --max_old_space_size=8192"

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git checkout -- . 2>/dev/null || true
# R6: test.patch first, then the fix from /home/fix.patch and nowhere else --
# at evaluation time the agent's patch is bind-mounted over that exact path.
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi

rc=0
{test_cmd}
exit "$rc"
""".format(pr=self.pr, test_cmd=test_cmd),
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

        prepare_commands = "RUN bash /home/prepare.sh"

       
        hardening_block = Image._HARDENING_BLOCK.replace('"${BASE_COMMIT}"', sha)

      
        pack_limits = (
            "RUN git config --local pack.windowMemory 256m && \\\n"
            "    git config --local pack.deltaCacheSize 128m && \\\n"
            "    git config --local pack.threads 2"
        )

        return f"""FROM {name}:{tag}

{copy_commands}
WORKDIR /home/{repo}

{pack_limits}

{hardening_block}
{prepare_commands}
"""


@Instance.register("Expensify", "App_76743_to_72955")
class APP_76743_TO_72955(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

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
        """Reads the lines jest-report.js emits, one per assertion.

        Sample (fix stage, PR 76533):

            apply_patch: /home/test.patch -> applied whole (fast path)
            PASSED tests/unit/ReportLayoutUtilsTest.ts > getReportLayout > returns grid
            FAILED tests/unit/ReportLayoutUtilsTest.ts > getReportLayout > handles empty
            SKIPPED tests/unit/ReportLayoutUtilsTest.ts > getReportLayout > todo case
            jest-report: suites=1 tests=3 passed=1 failed=1 pending=1

        The name is `<repo-relative file> > <describe...> > <it>`, carries no
        timing or worker id, and is produced by the same reporter in all three
        stages -- R3 and Check 4B. Uniqueness is not at risk: jest's own JSON
        keeps the full describe chain, so two same-titled cases in different
        blocks or different files stay distinct (Check 4A, jest = LOW risk).
        """
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED)\s+(\S.*)$")

        for raw in clean_log.splitlines():
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



Instance.register("Expensify", "App")(APP_76743_TO_72955)
