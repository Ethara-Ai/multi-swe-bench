from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: bigskysoftware/htmx PR 3828 (Jun 2026, branch four-dev, htmx 4.0.0-beta4).
#
# ERA: htmx 4. The test suite is Mocha + chai run in a real browser by
# @web/test-runner 0.20.2 through @web/test-runner-playwright (playwright 1.58.2,
# pinned by package-lock.json):
#   npm test = web-test-runner --browsers chromium
#              --config test/web-test-runner.config.mjs --playwright
# The config holds the whole test page (testRunnerHtml: chai, fetch-mock,
# src/htmx.js, helpers) and `files: ['test/tests/**/*.js']`. CI (ci.yml) runs
# `npm ci` then `npm test` on Node 20.x.
#
# htmx.py cannot grade this era: it picks its era from a version in the base label,
# and "bigskysoftware:four-dev" carries none, so it falls back to the htmx 1.x
# `mocha-chrome test/index.html` era; its web-test-runner era also omits --config.
#
# SHARED BASE architecture: image_tag() is PR-independent, so the base keeps FULL
# history and never references BASE_COMMIT; the pin lives in prepare.sh and the
# history scrub in the PR layer. The `# syntax` directive on line 1 of the base
# Dockerfile is what keeps DockerfileEnhancer from rewriting the clone into a
# pinned, scrubbed single commit.
BASE_TAG = "base-3828_to_3828"

# CI: actions/setup-node with node-version: '20.x'.
NODE_IMAGE = "node:20-bookworm"

REPO_DIR = "/home/htmx"

# Playwright's browser cache, fixed so the browser prepare.sh installs is the one
# every stage finds again.
PLAYWRIGHT_BROWSERS_PATH = "/opt/ms-playwright"

WTR_CONFIG = "test/web-test-runner.config.mjs"

# Wrapper config, COPY'd to /home: imports the repo's web-test-runner config
# unchanged, then narrows `files` to the graded files, turns coverage off (it is
# not graded) and swaps the console reporters for a structured one. Nothing in the
# repo is edited.
WTR_WRAPPER = "/home/argos_wtr.mjs"

# Marker line placed directly before the normalised report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@HTMX_WTR_JSON@@"

# File-level key: the browser session's own verdict for a test file. A file whose
# `before` hook fails (e.g. it loads an extension script only the fix adds)
# reports no test results at all; this key still records that it ran and failed.
FILE_TEST_NAME = "<file>"

# Recorded when web-test-runner produced no report at all (config failed to load,
# browser failed to launch). Only ever FAILED: a missing report is never a pass.
RUN_TEST_NAME = "<run>"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    for block in re.split(r"(?m)^(?=diff --git )", patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header:
            blocks.append((header.group(2), block))
    return blocks


def _test_patch_paths(pr: PullRequest) -> list[str]:
    """Post-patch paths the test patch touches; deleted files are dropped."""
    return sorted(
        {path for path, block in _patch_blocks(pr.test_patch) if "\ndeleted file mode" not in block}
    )


def binary_stub_paths(pr: PullRequest) -> list[str]:
    """Paths whose diff is a `Binary files ... differ` stub with no payload.

    git refuses such an entry and with it the whole patch, so these are excluded
    from every `git apply`. (None in PR 3828; kept so a stub can never silently
    drop a patch.)
    """
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


def test_files(pr: PullRequest) -> list[str]:
    """Test files the test patch touches, within the config's `test/tests/**/*.js`."""
    return [p for p in _test_patch_paths(pr) if p.startswith("test/tests/") and p.endswith(".js")]


class HtmxImageBase(Image):
    """Shared base: toolchain + full-history clone. No pin, no scrub."""

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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming
        # it is what would pin this shared image to one PR's commit.
        #
        # No browser is installed here: the Playwright release (and with it the
        # chromium build) is pinned per commit by package-lock.json, so prepare.sh
        # installs the one matching its commit. Playwright publishes chromium for
        # linux arm64 as well as amd64.
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    CI=true \\
    NODE_OPTIONS="--max-old-space-size=4096" \\
    PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_BROWSERS_PATH} \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    npm_config_update_notifier=false
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
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" {REPO_DIR}
WORKDIR {REPO_DIR}
{self.clear_env}
CMD ["/bin/bash"]
"""


CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

# ========== Section 1: PIN ==========
cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

# ========== Section 2: PROVISION ==========
node --version

# Exactly what CI runs. Not tolerated: a failed install is a failed build.
npm ci

# The chromium build matching this commit's Playwright (1.58.2 via the lockfile),
# plus its system libraries. CI's runners ship browsers; a container does not.
npx playwright install --with-deps chromium

# ========== Section 3: GATE (non-tolerant, last) ==========
test -f [[WTR_CONFIG]]
test -f src/htmx.js
# The runner and its Playwright launcher load (as ES modules, the way the config
# imports them -- neither package exports ./package.json)...
test -x node_modules/.bin/web-test-runner
node --input-type=module -e "const r = await import('@web/test-runner'); const p = await import('@web/test-runner-playwright'); if (typeof r.summaryReporter !== 'function' || typeof p.playwrightLauncher !== 'function') { throw new Error('web-test-runner did not load'); }"
# ...the browser they launch is installed and executable (checked, not started: a
# multi-arch build runs the other architecture under QEMU)...
node -e "const {chromium} = require('playwright'); require('fs').accessSync(chromium.executablePath(), require('fs').constants.X_OK)"
# ...and the repo's own test config loads.
node --input-type=module -e "const c = (await import('[[REPO_DIR]]/[[WTR_CONFIG]]')).default; if (!Array.isArray(c.files) || typeof c.testRunnerHtml !== 'function') { throw new Error('unexpected web-test-runner config'); }"
echo DEPS_OK
"""


# web-test-runner wrapper config (see WTR_WRAPPER). The reporter walks every
# browser session's Mocha result tree and writes one record per test.
WTR_MJS = r"""import fs from 'node:fs';
import upstream from '[[REPO_DIR]]/[[WTR_CONFIG]]';

const files = (process.env.ARGOS_WTR_FILES || '').split(/\s+/).filter(Boolean);

function walk(suite, chain, out) {
  if (!suite) return;
  const here = suite.name ? [...chain, suite.name] : chain;
  for (const t of suite.tests || []) {
    out.push({
      titles: [...here, String(t.name || '')],
      status: t.skipped ? 'skipped' : t.passed ? 'passed' : 'failed',
    });
  }
  for (const child of suite.suites || []) walk(child, here, out);
}

function argosReporter() {
  return {
    onTestRunFinished({sessions}) {
      const records = [];
      for (const s of sessions) {
        const tests = [];
        walk(s.testResults, [], tests);
        records.push({
          file: String(s.testFile || ''),
          passed: !!s.passed,
          errors: (s.errors || []).length,
          tests,
        });
      }
      fs.writeFileSync(process.env.ARGOS_WTR_REPORT, JSON.stringify({sessions: records}));
    },
  };
}

export default {
  ...upstream,
  rootDir: '[[REPO_DIR]]',
  files,
  coverage: false,
  reporters: [argosReporter()],
};
"""


# Normalises the reporter's records into one line of JSON after REPORT_MARKER.
# Names are `<file>::<describe> > ... > <title>`, never timing or counts, so the
# same test has the same name in every stage.
REPORT_CJS = r"""'use strict';
const fs = require('fs');

const ROOT = '[[REPO_DIR]]';
const [, , report, marker] = process.argv;

const tests = [];
const seen = new Map();
function add(name, status) {
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  tests.push({name: n > 1 ? `${name} #${n}` : name, status});
}
function rel(p) {
  p = String(p || '');
  return p.startsWith(ROOT + '/') ? p.slice(ROOT.length + 1) : p;
}

let ok = false;
try {
  if (fs.existsSync(report) && fs.statSync(report).size > 0) {
    for (const s of JSON.parse(fs.readFileSync(report, 'utf8')).sessions || []) {
      const file = rel(s.file);
      if (!file) continue;
      for (const t of s.tests || []) add(`${file}::${t.titles.join(' > ')}`, t.status);
      add(`${file}::[[FILE]]`, s.passed && !s.errors ? 'passed' : 'failed');
    }
    ok = true;
  }
} catch (err) {
  process.stderr.write(`report: ${err.message}\n`);
}
if (!ok) add('web-test-runner::[[RUN]]', 'failed');

console.log(marker);
console.log(JSON.stringify({argos_tests: tests}));
"""


# The one test command every stage runs. Each graded file runs only in stages where
# it exists: a test file the test patch creates is absent from the baseline, which
# then runs nothing for it.
TEST_CMD = r"""# A patch that changes the dependency manifests leaves node_modules from the base
# commit stale. Reinstall exactly when the applied patches touched them; the
# baseline and any patch that leaves them alone skip this. Not tolerated: a failed
# install fails the stage.
if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm ci
    npx playwright install chromium
fi

_report=/tmp/argos-wtr.json
rm -f "${_report}"

_files=()
for _f in [[TEST_FILES]]; do
    [ -f "${_f}" ] && _files+=("${_f}")
done

_rc=0
if [ "${#_files[@]}" -gt 0 ]; then
    # The same runner and browser as `npm test`; web-test-runner exits non-zero
    # when any test fails, and that status is the stage's.
    ARGOS_WTR_FILES="${_files[*]}" ARGOS_WTR_REPORT="${_report}" \
        npx web-test-runner --browsers chromium --playwright --config [[WTR_WRAPPER]] || _rc=$?
    node /home/argos_report.cjs "${_report}" "[[REPORT_MARKER]]"
else
    echo "[[REPORT_MARKER]]"
    echo '{"argos_tests":[]}'
fi
exit "${_rc}"
"""


class HtmxImageDefault(Image):
    """Per-PR layer: patches, scripts, the pin (via prepare.sh), and the scrub."""

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
        return HtmxImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[WTR_CONFIG]]", WTR_CONFIG)
            .replace("[[WTR_WRAPPER]]", WTR_WRAPPER)
            .replace("[[TEST_FILES]]", " ".join(f"'{p}'" for p in test_files(self.pr)))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[FILE]]", FILE_TEST_NAME)
            .replace("[[RUN]]", RUN_TEST_NAME)
        )

    def _apply(self, *patches: str) -> str:
        excludes = "".join(f" --exclude='{p}'" for p in binary_stub_paths(self.pr))
        return f"git apply --whitespace=nowarn{excludes} {' '.join(patches)}\n"

    def files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": self._apply("/home/test.patch"),
            "fix-run.sh": self._apply("/home/test.patch", "/home/fix.patch"),
        }
        stages = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\n" + TEST_CMD
                ),
            )
            for name, patch_step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
            File(".", "argos_wtr.mjs", self._expand(WTR_MJS)),
            File(".", "argos_report.cjs", self._expand(REPORT_CJS)),
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # PR images are never touched by DockerfileEnhancer (it returns raw the
        # moment dependency() is an Image), so everything this layer needs is
        # written out here. The scrub lives here rather than in the shared base:
        # the base must keep full history so every PR of the shard can reach its
        # own commit.
        #
        # The scrub deliberately contains no `git reset`, no `git clean` and no
        # path-scoped checkout -- prepare.sh is allowed to have modified tracked
        # files, and any of those would silently revert that work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
        return f"""FROM {name}:{tag}
{self.global_env}
{copies}RUN bash /home/prepare.sh
WORKDIR {REPO_DIR}
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{self.clear_env}
"""


@Instance.register("bigskysoftware", "htmx_3828_to_3828")
class HTMX_3828_TO_3828(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return HtmxImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        buckets: dict[str, set[str]] = {
            "passed": set(),
            "failed": set(),
            "skipped": set(),
        }

        # The report is the first line after the marker that parses as an object
        # carrying `argos_tests`. Test output can itself print `{`-lines, so a line
        # is only accepted once it parses. A truncated or missing report yields no
        # tests for this stage rather than names guessed from console text.
        report = None
        lines = log.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != REPORT_MARKER:
                continue
            for candidate in lines[index + 1 :]:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "argos_tests" in parsed:
                    report = parsed
                    break
            break

        for entry in (report or {}).get("argos_tests") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            status = str(entry.get("status") or "")
            if name and status in buckets:
                buckets[status].add(name)

        passed = buckets["passed"]
        failed = buckets["failed"]
        skipped = buckets["skipped"]

        # A name may live in only one bucket; failure wins over both others.
        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
