from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: postcss/autoprefixer PR 1560 (Oct 2026, main).
#
# ERA: at the base commit (c6d89bcf) the unit tests are plain uvu 0.5.6, CommonJS,
# one self-running file per module (`test.run()` at the bottom of each). CI's
# "Quick" jobs install with `pnpm install --frozen-lockfile --ignore-scripts`
# (pnpm major 12, pnpm-lock.yaml lockfileVersion 9.0) and run `pnpm unit`
# (`uvu . '\.test\.js$'`); the oldest-Node job runs `node test/<file>.test.js` per
# file. There is no jest anywhere in the repo, which is why autoprefixer.py (an
# `npx jest` config) cannot grade this PR.
#
# SHARED BASE architecture: image_tag() is PR-independent, so the base keeps FULL
# history and never references BASE_COMMIT; the pin lives in prepare.sh and the
# history scrub in the PR layer. The `# syntax` directive on line 1 of the base
# Dockerfile is what keeps DockerfileEnhancer from rewriting the clone into a
# pinned, scrubbed single commit.
BASE_TAG = "base-1560_to_1560"

# CI's Quick matrix runs Node 24 and 22 (the Full job Node 26); 24 is the current
# LTS of that matrix. package.json engines: "^10 || ^12 || >=14".
NODE_IMAGE = "node:24-bookworm"

# CI pins pnpm major 12 (`pnpm/setup ... version: 12`); the exact release is pinned
# here so every build resolves the same package manager.
PNPM_VERSION = "12.16.1"

REPO_DIR = "/home/autoprefixer"

# Marker line placed directly before the normalised report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@AUTOPREFIXER_UVU_JSON@@"

# File-level key: the test file's own exit status. uvu exits non-zero when any test
# or hook in the file fails, or when the file cannot even be loaded (e.g. it
# requires something only the fix adds) -- a case that reports no tests at all.
FILE_TEST_NAME = "<file>"

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
    from every `git apply`. (None in PR 1560; kept so a stub can never silently
    drop a patch.)
    """
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


def test_files(pr: PullRequest) -> list[str]:
    """The uvu files the test patch grades.

    - a test file itself: `test/<name>.test.js`;
    - a CSS case fixture under `test/cases/`: `test/autoprefixer.test.js`, the file
      that loads every case (`join(__dirname, '/cases/' + name + '.css')`).
    """
    files = set()
    for path in _test_patch_paths(pr):
        if not path.startswith("test/"):
            continue
        if path.endswith(".test.js"):
            files.add(path)
        elif path.startswith("test/cases/"):
            files.add("test/autoprefixer.test.js")
    return sorted(files)


class AutoprefixerImageBase(Image):
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
        # pnpm is a pure-JavaScript package (no native binary), so the npm-installed
        # release runs identically on amd64 and arm64.
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
    FORCE_COLOR=0 \\
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
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@{PNPM_VERSION} && pnpm --version
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
pnpm --version

# Exactly CI's Quick-job install. Not tolerated: a failed install is a failed build.
pnpm install --frozen-lockfile --ignore-scripts

# ========== Section 3: GATE (non-tolerant, last) ==========
# The test runner and the assertion module resolve from the repo...
node -e "require.resolve('uvu', {paths: ['[[REPO_DIR]]']}); require.resolve('uvu/assert', {paths: ['[[REPO_DIR]]']})"
# ...and the library loads from source and builds a working plugin.
node -e "const ap = require('[[REPO_DIR]]'); const p = require('[[REPO_DIR]]/node_modules/postcss'); const out = p([ap({overrideBrowserslist: ['Chrome 25']})]).process('a{display:flex}', {from: undefined}).css; if (!out.includes('-webkit-')) { throw new Error('autoprefixer did not prefix: ' + out); }"
echo DEPS_OK
"""


# Preloaded into every test file (`node --require`). uvu 0.5.6 has no reporter API:
# its own output is one dot per passing test, so names of passing tests never reach
# the log. This wraps the registration functions the test files call -- `test()`,
# `suite()`, `.only()`, `.skip()` -- so every handler records its own outcome under
# its own name. Nothing in the repo is edited.
UVU_PRELOAD_CJS = r"""'use strict';
const fs = require('fs');

const uvu = require(require.resolve('uvu', {paths: [process.cwd()]}));
const records = [];

function wrap(original, suiteName) {
  function record(name, status) {
    records.push({suite: suiteName, name: String(name), status});
  }
  function instrument(name, handler) {
    return async function (...args) {
      try {
        await handler(...args);
      } catch (err) {
        record(name, 'failed');
        throw err;
      }
      record(name, 'passed');
    };
  }
  const wrapped = (name, handler) => original(name, instrument(name, handler));
  for (const key of Object.keys(original)) {
    wrapped[key] = original[key];
  }
  wrapped.only = (name, handler) => original.only(name, instrument(name, handler));
  wrapped.skip = (name, ...rest) => {
    record(name, 'skipped');
    return original.skip(name, ...rest);
  };
  return wrapped;
}

const originalSuite = uvu.suite;
uvu.test = wrap(uvu.test, '');
uvu.suite = (name = '', state) => wrap(originalSuite(name, state), String(name));

process.on('exit', () => {
  fs.writeFileSync(process.env.ARGOS_UVU_REPORT, JSON.stringify({tests: records}));
});
"""


# Normalises the per-file records into one line of JSON after REPORT_MARKER. Names
# are `<file>::<suite> > <test>` (the suite part only for named uvu suites), never
# timing or counts, so the same test has the same name in every stage.
REPORT_CJS = r"""'use strict';
const fs = require('fs');

const [, , manifest, marker] = process.argv;
const tests = [];
const seen = new Map();
function add(name, status) {
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  tests.push({name: n > 1 ? `${name} #${n}` : name, status});
}

for (const line of fs.readFileSync(manifest, 'utf8').split('\n')) {
  if (!line.trim()) continue;
  const [file, report, rc] = line.split('\t');
  try {
    if (fs.existsSync(report) && fs.statSync(report).size > 0) {
      for (const t of JSON.parse(fs.readFileSync(report, 'utf8')).tests || []) {
        const title = t.suite ? `${t.suite} > ${t.name}` : t.name;
        add(`${file}::${title}`, t.status);
      }
    }
  } catch (err) {
    process.stderr.write(`report: ${file}: ${err.message}\n`);
  }
  add(`${file}::[[FILE]]`, rc === '0' ? 'passed' : 'failed');
}

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
if ! git diff --quiet HEAD -- package.json pnpm-lock.yaml; then
    pnpm install --frozen-lockfile --ignore-scripts
fi

_reports=/tmp/argos-reports
rm -rf "${_reports}"
mkdir -p "${_reports}"
: > "${_reports}/manifest.tsv"

# Every file's exit status is collected rather than aborting on the first: all
# files must report, and the stage fails if any of them failed.
_rc=0
for _f in [[TEST_FILES]]; do
    [ -f "${_f}" ] || continue
    _report="${_reports}/$(echo "${_f}" | tr '/' '_').json"
    _frc=0
    ARGOS_UVU_REPORT="${_report}" node --require /home/argos_uvu.cjs "${_f}" || _frc=$?
    printf '%s\t%s\t%s\n' "${_f}" "${_report}" "${_frc}" >> "${_reports}/manifest.tsv"
    [ "${_frc}" -eq 0 ] || _rc=1
done

node /home/argos_report.cjs "${_reports}/manifest.tsv" "[[REPORT_MARKER]]"
exit "${_rc}"
"""


class AutoprefixerImageDefault(Image):
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
        return AutoprefixerImageBase(self.pr, self._config)

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
            .replace("[[FILE]]", FILE_TEST_NAME)
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
            File(".", "argos_uvu.cjs", UVU_PRELOAD_CJS),
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


@Instance.register("postcss", "autoprefixer_1560_to_1560")
class AUTOPREFIXER_1560_TO_1560(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AutoprefixerImageDefault(self.pr, self._config)

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
