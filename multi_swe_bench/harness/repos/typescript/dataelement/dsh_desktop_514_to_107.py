from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: dataelement/dsh-desktop PRs 107..514 (Aug-Sep 2026, main).
#
# ERA: one toolchain across every base commit of the interval (11975855 .. 6053914c):
#   - npm with package-lock.json (lockfileVersion 3), installed by `npm ci`;
#   - unit tests `vitest run` (vitest ^4.1.10), TypeScript test files transformed
#     by Vitest itself -- no build step;
#   - postinstall `patch-package && node scripts/install-brand-assets.mjs &&
#     install-electron --no` (applies patches/, copies local logos into
#     node_modules, fetches the Electron 43 binary);
#   - CI (release.yml) runs `npm ci` then `npm test` on Node 22.
# The only difference is that vitest.config.ts exists from 352 onward (include
# test/**, fileParallelism, testTimeout 60s); earlier commits use Vitest's defaults,
# and Vitest loads whichever the commit has. No toolchain migration, so one era.
#
# SHARED BASE architecture: image_tag() is PR-independent, so the base keeps FULL
# history and never references BASE_COMMIT; the pin lives in prepare.sh and the
# history scrub in the PR layer. The `# syntax` directive on line 1 of the base
# Dockerfile is what keeps DockerfileEnhancer from rewriting the clone into a
# pinned, scrubbed single commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-514_to_107"

# CI's test jobs all run `actions/setup-node` with node-version: 22.
NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/dsh-desktop"

# Marker line placed directly before the normalised report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@DSH_DESKTOP_VITEST_JSON@@"

# Suite-level key: emitted from Vitest's per-file verdict. A test file whose module
# fails to load (e.g. it imports something only the fix adds) reports no
# assertions at all; this key still records that the file ran and how it ended.
SUITE_TEST_NAME = "<suite>"

# Recorded when Vitest produced no report at all (config failed to load, crash
# before reporting). Only ever FAILED: a missing report is never a pass.
RUN_TEST_NAME = "<run>"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_TEST_FILE_RE = re.compile(r"^test/.+\.(?:test|spec)\.(?:ts|js|mjs)$")


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

    git refuses such an entry ("cannot apply binary patch ... without full index
    line") and with it the WHOLE patch, so these paths are excluded from every
    `git apply`. In this dataset they are images vendored with a package
    (PR 352), which no graded test reads.
    """
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


def test_files(pr: PullRequest) -> list[str]:
    """Root-suite test files the test patch touches.

    Only `test/**/*.{test,spec}.{ts,js,mjs}` -- the root vitest.config.ts include
    pattern (and the subset of Vitest's defaults the repo uses before that file
    existed). Configs, docs and fixtures in the patch select nothing; tests of
    vendored packages under packages/ belong to those packages' own suites.
    """
    return [p for p in _test_patch_paths(pr) if _TEST_FILE_RE.match(p)]


class DshDesktopImageBase(Image):
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
        # build-essential + python3: node-pty (and any native addon without a
        # matching prebuild) compiles through node-gyp during `npm ci`. Every other
        # native dependency ships prebuilt linux-x64 and linux-arm64 packages
        # (esbuild, rollup, koffi, sharp, @deepseek-ai/node-addon-*-linux-*), and
        # Electron publishes linux builds for both arches.
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
        git ca-certificates build-essential python3 unzip zip xz-utils \\
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
npm --version

# Exactly what CI runs, postinstall included (patch-package applies patches/, the
# brand script copies local logos into node_modules). Not tolerated: a failed
# install is a failed build.
npm ci

# ========== Section 3: GATE (non-tolerant, last) ==========
npx vitest --version
# Vitest's TypeScript transform (esbuild, a per-arch native binary) actually runs.
node -e "const out = require('esbuild').transformSync('const n: number = 1; export default n', {loader: 'ts'}).code; if (!out.includes('const n = 1')) { throw new Error('esbuild transform broken: ' + out); }"
echo DEPS_OK
"""


# Normalises Vitest's own JSON report into one line of JSON, printed after
# REPORT_MARKER. Names are `<file>::<describe> > ... > <title>`: the file plus the
# whole describe chain, never timing or counts, so the same test has the same name
# in every stage.
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
const OUTCOME = {
  passed: 'passed',
  failed: 'failed',
  skipped: 'skipped',
  pending: 'skipped',
  todo: 'skipped',
  disabled: 'skipped',
};

let ok = false;
try {
  if (fs.existsSync(report) && fs.statSync(report).size > 0) {
    const data = JSON.parse(fs.readFileSync(report, 'utf8'));
    for (const suite of data.testResults || []) {
      const path = rel(suite.name);
      if (!path) continue;
      for (const a of suite.assertionResults || []) {
        const status = OUTCOME[String(a.status || '')];
        if (!status) continue;
        const titles = [...(a.ancestorTitles || []), String(a.title || '')];
        add(`${path}::${titles.join(' > ')}`, status);
      }
      if (suite.status === 'passed' || suite.status === 'failed') {
        add(`${path}::[[SUITE]]`, suite.status);
      }
    }
    ok = true;
  }
} catch (err) {
  process.stderr.write(`report: ${err.message}\n`);
}
if (!ok) add('vitest::[[RUN]]', 'failed');

console.log(marker);
console.log(JSON.stringify({argos_tests: tests}));
"""


# The one test command every stage runs. Each graded file runs only in stages where
# it exists: a test file the test patch creates is absent from the baseline, which
# then runs nothing for it.
TEST_CMD = r"""# A patch that changes the dependency manifests (107, 352 and 447 change
# package.json; 352 and 447 also the lockfile) leaves node_modules from the base
# commit stale. Reinstall exactly when the applied patches touched them; the
# baseline and any patch that leaves them alone skip this. Not tolerated: a failed
# install fails the stage.
if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm ci
fi

_report=/tmp/argos-vitest.json
rm -f "${_report}"

_files=()
for _f in [[TEST_FILES]]; do
    [ -f "${_f}" ] && _files+=("${_f}")
done

_rc=0
if [ "${#_files[@]}" -gt 0 ]; then
    # Vitest loads the commit's own vitest.config.ts when it has one. The JSON
    # report goes to a file; Vitest exits non-zero when any test fails, and that
    # status is the stage's.
    npx vitest run --reporter=json --outputFile="${_report}" "${_files[@]}" || _rc=$?
    node /home/argos_report.cjs "${_report}" "[[REPORT_MARKER]]"
else
    echo "[[REPORT_MARKER]]"
    echo '{"argos_tests":[]}'
fi
exit "${_rc}"
"""


class DshDesktopImageDefault(Image):
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
        return DshDesktopImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_FILES]]", " ".join(f"'{p}'" for p in test_files(self.pr)))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[SUITE]]", SUITE_TEST_NAME)
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


@Instance.register("dataelement", "dsh_desktop_514_to_107")
class DSH_DESKTOP_514_TO_107(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return DshDesktopImageDefault(self.pr, self._config)

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
