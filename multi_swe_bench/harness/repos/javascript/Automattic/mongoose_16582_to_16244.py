from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: Automattic/mongoose PRs 16244..16582 (Apr-Oct 2026; branches master,
# 9.8, 9.10, 9.11 and 10.0 -- mongoose 9.6 .. 9.11 at the base commits).
#
# ERA: one toolchain across every base commit of the interval:
#   - npm, NO lockfile (.npmrc: package-lock=false, ignore-scripts=true,
#     min-release-age=2); CI runs `npm install`;
#   - runtime tests: mocha 12 (beta-10 .. 12.0.2) with .mocharc.yml (spec reporter,
#     `require: test/mocha-fixtures.js`), whose global setup starts a MongoDB
#     through mongodb-memory-server 11 unless MONGOOSE_TEST_URI is set;
#   - type tests: tstyche 7 (`npm run test:types`, tstyche.json testFileMatch
#     test/types/**/*.test.*) against the pinned typescript;
#   - CI (test.yml / types.yml): Node 22 for lint/types, a 20/22/24 x MongoDB
#     6.0.15/7.0.12/8.2.0 matrix on Ubuntu 22.04/24.04 for tests, each run after
#     `npm run create-separate-require-instance`.
# The `npm test` glob widened from ./test/*.test.js to ./test/**/*.test.js (minus
# test/encryption) in Aug 2026; the runner did not change, and this config runs the
# graded files explicitly, so it is one era.
#
# mongoose.py cannot grade this interval: it runs the whole `npm test` (no type
# tests at all -- 16500 and 16523 change only test/types/queries.test.ts), installs
# with `npm install || true` and no gate, and lets mongodb-memory-server download a
# MongoDB at test time, which has no Debian arm64 build.
#
# SHARED BASE architecture: image_tag() is PR-independent, so the base keeps FULL
# history and never references BASE_COMMIT; the pin lives in prepare.sh and the
# history scrub in the PR layer. The `# syntax` directive on line 1 of the base
# Dockerfile is what keeps DockerfileEnhancer from rewriting the clone into a
# pinned, scrubbed single commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-16582_to_16244"

# CI's lint and type jobs, and the coverage leg of the test matrix, use Node 22.
NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/mongoose"

# MongoDB for the test fixtures: one of CI's matrix versions, baked into the base so
# no test stage downloads anything. MongoDB publishes Ubuntu 22.04 builds for both
# x86_64 and aarch64 (Debian has no arm64 build); they run on Debian 12, whose
# glibc 2.36 is newer than 22.04's 2.35. Checksums are MongoDB's own published
# .sha256 files.
MONGODB_VERSION = "7.0.12"
MONGODB_SHA256 = {
    "amd64": ("x86_64", "2a0abaeab3812a0348570e9bbf336ba67191c72a0108fd005a77f3b3d45e5559"),
    "arm64": ("aarch64", "38bc4fa4060589c5aba91268ddc3481b96344ba30831ddaf5bc6e5b9092a9f29"),
}
MONGOD_BIN = "/opt/mongodb/bin/mongod"

# Dependencies are resolved as of the base commit's own date: with no lockfile, a
# plain `npm install` would resolve today's releases against months-old source.
# prepare.sh records the date here and every reinstall reuses it.
NPM_BEFORE_FILE = "/home/argos_npm_before"

# Marker line placed directly before the normalised report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@MONGOOSE_TEST_JSON@@"

# File-level key: the runner's own exit status for a test file. A file that cannot
# be loaded (it requires something only the fix adds), or whose global setup fails,
# reports no test results at all; this key still records that it ran and failed.
FILE_TEST_NAME = "<file>"

# Recorded when tstyche produced no report at all. Only ever FAILED.
RUN_TEST_NAME = "<run>"

# mocha's own runtime timeout for CI (`npm run test:ci` adds --timeout 10000).
MOCHA_TIMEOUT_MS = 10000

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
    from every `git apply`. (None in this interval; kept so a stub can never
    silently drop a patch.)
    """
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


def mocha_test_files(pr: PullRequest) -> list[str]:
    """Runtime test files: `npm test`'s test/**/*.test.js, minus test/encryption
    (needs a separately provisioned encryption setup, excluded by `npm test`
    itself) and test/types (type tests, run by tstyche)."""
    return [
        p
        for p in _test_patch_paths(pr)
        if p.startswith("test/")
        and p.endswith(".test.js")
        and not p.startswith(("test/encryption/", "test/types/"))
    ]


def type_test_files(pr: PullRequest) -> list[str]:
    """Type test files: tstyche.json's test/types/**/*.test.*, as TypeScript."""
    return [p for p in _test_patch_paths(pr) if p.startswith("test/types/") and p.endswith(".test.ts")]


class MongooseImageBase(Image):
    """Shared base: toolchain + MongoDB + full-history clone. No pin, no scrub."""

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
        (x64_arch, x64_sha) = MONGODB_SHA256["amd64"]
        (arm_arch, arm_sha) = MONGODB_SHA256["arm64"]

        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming
        # it is what would pin this shared image to one PR's commit.
        #
        # The MongoDB download is selected by TARGETARCH and verified against the
        # pinned checksum; any other architecture fails the build loudly.
        # libcurl4/libssl3/liblzma5 are mongod's runtime libraries.
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
    MONGOMS_VERSION={MONGODB_VERSION} \\
    MONGOMS_SYSTEM_BINARY={MONGOD_BIN} \\
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
        git ca-certificates curl libcurl4 libssl3 liblzma5 \\
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \\
    case "${{TARGETARCH:-$(dpkg --print-architecture)}}" in \\
        amd64) mdb_arch={x64_arch}; mdb_sha={x64_sha} ;; \\
        arm64) mdb_arch={arm_arch}; mdb_sha={arm_sha} ;; \\
        *) echo "no MongoDB build for ${{TARGETARCH}}" >&2; exit 1 ;; \\
    esac; \\
    tgz="mongodb-linux-${{mdb_arch}}-ubuntu2204-{MONGODB_VERSION}.tgz"; \\
    curl -fsSL -o "/tmp/${{tgz}}" "https://fastdl.mongodb.org/linux/${{tgz}}"; \\
    echo "${{mdb_sha}}  /tmp/${{tgz}}" | sha256sum -c -; \\
    mkdir -p /opt/mongodb; \\
    tar -xzf "/tmp/${{tgz}}" -C /opt/mongodb --strip-components=1; \\
    rm -f "/tmp/${{tgz}}"; \\
    test -x {MONGOD_BIN}
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

# No lockfile at any commit of this interval, so resolution is pinned to the base
# commit's own date instead: every dependency resolves to what it resolved to when
# this commit was written. The repo's .npmrc still applies (ignore-scripts=true,
# package-lock=false). Not tolerated: a failed install is a failed build.
git show -s --format=%cI HEAD > [[NPM_BEFORE]]
npm install --before="$(cat [[NPM_BEFORE]])"

# What CI runs before the tests: a packed copy of this checkout installed as
# node_modules/mongoose-separate-require-instance (used by the multiple-instances
# tests).
npm run create-separate-require-instance

# ========== Section 3: GATE (non-tolerant, last) ==========
# The library loads from source, with its driver...
node -e "const m = require('[[REPO_DIR]]'); if (typeof m.connect !== 'function' || typeof m.Schema !== 'function') { throw new Error('mongoose did not load'); } require('mongodb')"
# ...both runners and the fixtures' MongoDB launcher resolve...
node -e "for (const p of ['mocha/package.json', 'tstyche/package.json', 'mongodb-memory-server', 'typescript']) require.resolve(p)"
test -d node_modules/mongoose-separate-require-instance
# ...and the MongoDB binary they will start is installed (checked, not started: a
# multi-arch build runs the other architecture under QEMU, which mongod's x86 build
# does not support).
test -x "${MONGOMS_SYSTEM_BINARY}"
echo DEPS_OK
"""


# tstyche reporter, COPY'd to /home and passed with --reporters. tstyche calls
# on([event, payload]) for every event; each TestResult carries its test node and a
# parent chain of DescribeResults, so names keep the whole describe context.
#
# mongoose's type tests are mostly bare `expect(...)` assertions inside plain
# functions, outside any test() block: tstyche reports those only as expect:* events.
# A failing or erroring assertion therefore fails its file even when no test:fail
# ever fires -- otherwise a file with 7 failed assertions reads as passed.
TSTYCHE_MJS = r"""import fs from 'node:fs';

const OUTCOME = {
  'test:pass': 'passed',
  'test:fail': 'failed',
  'test:error': 'failed',
  'test:skip': 'skipped',
  'test:todo': 'skipped',
  'test:fixme': 'skipped',
};

export default class ArgosReporter {
  #file;
  #files = [];

  on([event, payload]) {
    if (event === 'file:start') {
      const file = payload.result.file;
      this.#file = {path: String(file && file.path != null ? file.path : file), error: false, failedAssertions: 0, tests: []};
      this.#files.push(this.#file);
    } else if (event === 'file:error' || event === 'collect:error' || event === 'directive:error') {
      if (this.#file) this.#file.error = true;
    } else if (event === 'expect:fail' || event === 'expect:error') {
      if (this.#file) this.#file.failedAssertions++;
    } else if (event in OUTCOME && this.#file) {
      const result = payload.result;
      const titles = [String(result.test && result.test.name)];
      for (let p = result.parent; p && p.describe; p = p.parent) {
        titles.unshift(String(p.describe.name));
      }
      this.#file.tests.push({titles, status: OUTCOME[event]});
    } else if (event === 'run:end') {
      fs.writeFileSync(process.env.ARGOS_TSTYCHE_REPORT, JSON.stringify({files: this.#files}));
    }
  }
}
"""


# Normalises every runner's own structured report into one line of JSON, printed
# after REPORT_MARKER. Names are `<file>::<describe chain> <title>` -- the file plus
# the runner's full title, never timing or counts -- so the same test has the same
# name in every stage.
REPORT_CJS = r"""'use strict';
const fs = require('fs');

const ROOT = '[[REPO_DIR]]';
const [, , manifest, marker] = process.argv;

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
function readJson(file) {
  return fs.existsSync(file) && fs.statSync(file).size > 0 ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
}

function mocha(file, report, rc) {
  try {
    const data = readJson(report);
    if (data) {
      // Mocha's fullTitle is the describe chain and the title joined by spaces.
      for (const [key, status] of [['passes', 'passed'], ['failures', 'failed'], ['pending', 'skipped']]) {
        for (const t of data[key] || []) {
          add(`${rel(t.file) || file}::${String(t.fullTitle || t.title || '')}`, status);
        }
      }
    }
  } catch (err) {
    process.stderr.write(`report: ${file}: ${err.message}\n`);
  }
  add(`${file}::[[FILE]]`, rc === '0' ? 'passed' : 'failed');
}

function tstyche(report) {
  let data = null;
  try {
    data = readJson(report);
  } catch (err) {
    process.stderr.write(`report: tstyche: ${err.message}\n`);
  }
  if (!data) {
    add('test/types::[[RUN]]', 'failed');
    return;
  }
  for (const f of data.files || []) {
    const file = rel(f.path);
    let failed = f.error || f.failedAssertions > 0;
    for (const t of f.tests || []) {
      add(`${file}::${t.titles.join(' > ')}`, t.status);
      if (t.status === 'failed') failed = true;
    }
    add(`${file}::[[FILE]]`, failed ? 'failed' : 'passed');
  }
}

for (const line of fs.readFileSync(manifest, 'utf8').split('\n')) {
  if (!line.trim()) continue;
  const [kind, file, report, rc] = line.split('\t');
  if (kind === 'mocha') mocha(file, report, rc);
  else if (kind === 'tstyche') tstyche(report);
}

console.log(marker);
console.log(JSON.stringify({argos_tests: tests}));
"""


# The one test command every stage runs. Each graded file runs only in stages where
# it exists: a test file the test patch creates is absent from the baseline, which
# then runs nothing for it.
TEST_CMD = r"""# A patch that changes package.json (16392, 16455 and 16567 do) leaves node_modules
# from the base commit stale. Reinstall exactly when the applied patches touched it,
# resolving as of the same base-commit date as prepare.sh; the baseline and any patch
# that leaves it alone skip this. Not tolerated: a failed install fails the stage.
if ! git diff --quiet HEAD -- package.json; then
    npm install --before="$(cat [[NPM_BEFORE]])"
fi

# As CI does before every test run: re-pack the checkout -- now including the
# applied patches -- as node_modules/mongoose-separate-require-instance.
npm run create-separate-require-instance > /dev/null

_reports=/tmp/argos-reports
rm -rf "${_reports}"
mkdir -p "${_reports}"
: > "${_reports}/manifest.tsv"

# Every runner's exit status is collected rather than aborting on the first: all
# files must report, and the stage fails if any of them failed.
_rc=0

# Runtime tests: one mocha process per file, so a file that cannot even load does
# not take the other graded files down with it. .mocharc.yml still applies
# (test/mocha-fixtures.js starts the MongoDB); only the reporter is replaced.
for _f in [[MOCHA_FILES]]; do
    [ -f "${_f}" ] || continue
    _report="${_reports}/mocha-$(echo "${_f}" | tr '/' '_').json"
    _frc=0
    npx mocha --exit --timeout [[MOCHA_TIMEOUT]] --reporter json \
        --reporter-option output="${_report}" "${_f}" || _frc=$?
    printf 'mocha\t%s\t%s\t%s\n' "${_f}" "${_report}" "${_frc}" >> "${_reports}/manifest.tsv"
    [ "${_frc}" -eq 0 ] || _rc=1
done

# Type tests: one tstyche run over every graded type file (tstyche.json and
# test/types/tsconfig.json still apply).
_types=()
for _f in [[TYPE_FILES]]; do
    [ -f "${_f}" ] && _types+=("${_f}")
done
if [ "${#_types[@]}" -gt 0 ]; then
    _report="${_reports}/tstyche.json"
    ARGOS_TSTYCHE_REPORT="${_report}" npx tstyche --reporters /home/argos_tstyche.mjs \
        "${_types[@]}" || _rc=1
    printf 'tstyche\ttest/types\t%s\t-\n' "${_report}" >> "${_reports}/manifest.tsv"
fi

node /home/argos_report.cjs "${_reports}/manifest.tsv" "[[REPORT_MARKER]]"
exit "${_rc}"
"""


class MongooseImageDefault(Image):
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
        return MongooseImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[NPM_BEFORE]]", NPM_BEFORE_FILE)
            .replace("[[MOCHA_FILES]]", " ".join(f"'{p}'" for p in mocha_test_files(self.pr)))
            .replace("[[TYPE_FILES]]", " ".join(f"'{p}'" for p in type_test_files(self.pr)))
            .replace("[[MOCHA_TIMEOUT]]", str(MOCHA_TIMEOUT_MS))
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
            File(".", "argos_tstyche.mjs", TSTYCHE_MJS),
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


@Instance.register("Automattic", "mongoose_16582_to_16244")
class MONGOOSE_16582_TO_16244(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MongooseImageDefault(self.pr, self._config)

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
