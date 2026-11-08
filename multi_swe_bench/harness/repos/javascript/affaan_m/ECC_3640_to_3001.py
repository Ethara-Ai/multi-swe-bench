"""affaan-m/ECC -- raw single-PR records (PRs 3001..3640, Oct 2026).

The upstream repository was renamed from `everything-claude-code` to `ECC`. The
records of this dataset therefore carry `repo: "ECC"` and no `number_interval`, so
Instance.create routes them to `affaan-m/ECC`, which is what this module registers.
The older bundle dataset (`repo: "everything-claude-code"`, PRs 31..548) keeps its
own config in everything_claude_code.py; nothing here changes it.

ERA: at every base commit of this dataset (13 distinct shas, 2d515e4f..6c557856)
  * tests run through the repo's own runner `node tests/run-all.js`, which walks
    tests/**/*.test.js, runs each file in its own `node` process and prints
        ━━━ Running <file> ━━━          (section header, column 0)
          ✓ <name> / ✗ <name>           (custom harness, indented)
          PASS <name> / FAIL <name>     (custom harness, indented)
        ok N - <name> / not ok N - <name>   (node:test TAP)
        ✗ <file> exited with status N   (file-level verdict, column 0)
  * `.tool-versions` pins nodejs 20.19.0 and package.json `engines.node` is >=18;
  * upstream CI installs with `npm ci --ignore-scripts` and then runs
    `node tests/run-all.js` (.github/workflows/ci.yml, npm lane). Both are used
    here unchanged.

SHARED BASE architecture: image_tag() is PR-independent ("base-3640_to_3001"), so
one base image serves all 20 PRs. The base keeps FULL history and never references BASE_COMMIT;
the pin lives in prepare.sh and the history scrub in the PR layer. Line 1 of the
base Dockerfile is the `# syntax` directive, which keeps DockerfileEnhancer from
rewriting the clone into a pinned single-commit image (that would make every PR
but one unbuildable).
"""

from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Only the public classes are re-exported by the package's star-imports; the module
# constants (PREPARE_SH, TEST_CMD, ...) share names with other repo configs.
__all__ = ["ECC_3640_TO_3001", "EccImageBase", "EccImageDefault", "parse_ecc_log"]

# Shared base for this era-shard, named like the file: PRs 3640..3001. A
# PR-independent tag is what makes the base shared; the interval keeps it distinct
# from any other ECC era config added later (QC 2F).
BASE_TAG = "base-3640_to_3001"

# `.tool-versions`: nodejs 20.19.0 at every base commit of this dataset.
NODE_IMAGE = "node:20-bookworm"

# Unprivileged account the suite runs under. Several tests assert real EACCES
# behaviour by chmod-ing a file to 0444 and expecting the write to be refused; root
# bypasses permission bits, so as root those tests fail in every stage and can never
# carry a signal. Running as a normal user makes them behave as written.
TEST_USER = "ecc"
TEST_UID = "1500"

# Suite-level key: one per test file, from run-all.js's own per-file verdict. A test
# file that cannot even load (e.g. it requires a module only the fix adds) reports
# no assertions at all; this key still records that the file ran and how it ended.
FILE_TEST_SUFFIX = "<file>"

# Python lane. The repo also ships the `llm-abstraction` package (pyproject.toml,
# src/llm, requires-python >=3.11) with its own pytest suite; upstream CI runs it in a
# separate "python-tests" job:  pip install -e '.[dev]'  then
# pytest tests/test_*.py -m "not integration". Some PRs (#3272 entirely) are Python
# changes graded only by those tests, so every stage runs both suites.
PY_VENV = "/opt/ecc-venv"
PYTEST_MARKER = "@@ECC_PYTEST@@"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_BINARY_STUB_RE = re.compile(r"^Binary files .* differ$")


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    for block in re.split(r"(?m)^(?=diff --git )", patch or ""):
        m = _DIFF_HEADER_RE.match(block)
        if m:
            blocks.append((m.group(2), block))
    return blocks


def binary_stub_paths(pr: PullRequest) -> list[str]:
    """Paths whose diff block is a header-only binary stub.

    A `Binary files ... differ` block without a `GIT binary patch` payload cannot be
    applied ("cannot apply binary patch ... without full index line"), and git apply
    is atomic, so one such block would stop the whole patch. Such stubs are vendored
    assets no test reads; they are excluded by path, so every text change still
    applies exactly as recorded. None of the 20 records of this dataset has one
    today; the guard keeps the config correct for records that do.
    """
    paths = []
    for path, block in _patch_blocks(pr.test_patch) + _patch_blocks(pr.fix_patch):
        # Only the block HEADER decides: a text file may itself contain the words
        # "Binary files" (#3495 edits a doc that does), and its hunks start at "@@".
        header = block.split("\n@@", 1)[0].splitlines()
        if any(_BINARY_STUB_RE.match(l) for l in header) and not any(
            l == "GIT binary patch" for l in header
        ):
            paths.append(path)
    return sorted(set(paths))


class EccImageBase(Image):
    """Shared base: toolchain + full-history clone, pinned to nothing."""

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
        org = self.pr.org
        repo = self.pr.repo
        # ARG BASE_COMMIT is DECLARED (the harness passes it to every build and
        # BuildKit warns about unused args) but never REFERENCED below. Consuming it
        # is what would pin this shared image to one PR's commit.
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}
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
        git ca-certificates python3 python3-venv jq ripgrep \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN useradd -m -u {TEST_UID} {TEST_USER}
RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null
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


# npm rewrites a checked-in yarn.lock into its own format on every install (this repo
# ships both lockfiles; packageManager is yarn). That would leave yarn.lock dirty in
# the shipped image, and #3280/#3479/#3495 patch yarn.lock, so their patches would
# no longer apply. The tracked file is put back byte-for-byte after each install.
_KEEP_YARN_LOCK_BEGIN = r"""_yarn_lock_copy=""
if [ -f yarn.lock ]; then
    _yarn_lock_copy="$(mktemp)"
    cp yarn.lock "${_yarn_lock_copy}"
fi"""

_KEEP_YARN_LOCK_END = r"""if [ -n "${_yarn_lock_copy}" ]; then
    cp "${_yarn_lock_copy}" yarn.lock
    rm -f "${_yarn_lock_copy}"
fi"""


# ---------------------------------------------------------------------------
# prepare.sh -- three sections: PIN, PROVISION, GATE.
# ---------------------------------------------------------------------------
PREPARE_SH = (
    r"""#!/bin/bash
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

# Exactly what upstream CI runs, against this commit's package-lock.json. Not
# tolerated: a failed install is a failed build.
"""
    + _KEEP_YARN_LOCK_BEGIN
    + r"""
npm ci --ignore-scripts
"""
    + _KEEP_YARN_LOCK_END
    + r"""

# The Python package and its test dependencies, exactly as upstream CI's
# python-tests job installs them (bookworm's python3 is 3.11, which the package
# requires). A venv, because Debian's system Python is externally managed. The
# package has no lockfile, so this resolves the newest releases its ranges allow.
# Not tolerated: a failed install is a failed build.
python3 -m venv [[PY_VENV]]
[[PY_VENV]]/bin/python -m pip install --no-cache-dir --upgrade pip
[[PY_VENV]]/bin/python -m pip install --no-cache-dir -e '.[dev]'

# ========== Section 3: GATE (non-tolerant, last) ==========
# The runner every stage calls exists at this commit.
test -f tests/run-all.js
# Every declared dependency -- runtime and test-only (c8, eslint, typescript, ...) --
# is installed, and the test account exists.
node -e "const p = require('./package.json'); const fs = require('fs'); const deps = Object.keys(Object.assign({}, p.dependencies, p.devDependencies)); for (const d of deps) { if (!fs.existsSync('node_modules/' + d + '/package.json')) { throw new Error('missing dependency ' + d); } } console.log('deps: ' + deps.length);"
id -u [[USER]]
# System tools the suite shells out to (upstream CI installs both): the skill-
# stocktake scripts need jq, the secret-scanner fixtures need ripgrep with PCRE2.
jq --version
rg --pcre2 --version
# The Python lane: the package imports from source and the test runner loads.
[[PY_VENV]]/bin/python -c "import llm, pytest; print('llm', llm.__file__); print('pytest', pytest.__version__)"
echo DEPS_OK
"""
)


# The one test command every stage runs.
TEST_CMD = (
    r"""# A patch that changes the root dependencies leaves node_modules from the base commit
# stale. Reinstall exactly when the applied patches changed a dependency section of
# package.json or package-lock.json itself -- many fixes here only edit package.json's
# "files" list, which needs no install. The baseline and patches that leave the
# dependencies alone skip this. Not tolerated: a failed install fails the stage.
if [ "$(node -e "
const cp = require('child_process');
const keys = ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'];
const pick = (j) => JSON.stringify(keys.map((k) => j[k] || {}));
const base = JSON.parse(cp.execSync('git show HEAD:package.json', { encoding: 'utf8' }));
const now = require('./package.json');
const lockChanged = cp.spawnSync('git', ['diff', '--quiet', 'HEAD', '--', 'package-lock.json']).status !== 0;
console.log(lockChanged || pick(base) !== pick(now) ? 'yes' : 'no');
")" = "yes" ]; then
"""
    + _KEEP_YARN_LOCK_BEGIN
    + r"""
    if git diff --quiet HEAD -- package-lock.json; then
        npm install --ignore-scripts
    else
        npm ci --ignore-scripts
    fi
"""
    + _KEEP_YARN_LOCK_END
    + r"""
fi

# Run as the unprivileged test account (see TEST_USER). Patches were applied as
# root, so ownership is handed over first; this happens identically in all three
# stages, so the stages stay comparable.
chown -R [[USER]]:[[USER]] [[REPO_DIR]]

# The runner's output goes to a file first and is printed once it has finished. At
# some base commits (ef648e01: #3220/#3272/#3416/#3464) writing it straight to the
# container's stdout pipe silently loses everything after ~131 KB -- the suite runs
# to completion and exits 0, but the log stops 62 files in, so most tests are never
# recorded. Written to a file, the same run is complete. The runner's exit status is
# kept and returned, not swallowed.
_out=/tmp/ecc-run-all.log
_rc=0
runuser -u [[USER]] -- env HOME=/home/[[USER]] CI=true \
    timeout --kill-after=30 1500 node tests/run-all.js > "${_out}" 2>&1 || _rc=$?
cat "${_out}"

# Python lane, after the JS runner, through a file for the same reason. JUnit XML is
# the source of truth; ecc_pytest_report.py turns it into one marker line per test.
# --continue-on-collection-errors: a test file that cannot import yet (it imports
# what only the fix adds) is recorded as failed instead of aborting the session.
_py_out=/tmp/ecc-pytest.log
_py_xml=/tmp/ecc-pytest.xml
rm -f "${_py_xml}"
runuser -u [[USER]] -- env HOME=/home/[[USER]] CI=true PYTHONDONTWRITEBYTECODE=1 \
    timeout --kill-after=30 900 [[PY_VENV]]/bin/python -m pytest tests/test_*.py \
    -m "not integration" -p no:cacheprovider --continue-on-collection-errors \
    -o junit_family=xunit1 --junitxml="${_py_xml}" -q > "${_py_out}" 2>&1 || _rc=$?
cat "${_py_out}"
[[PY_VENV]]/bin/python /home/ecc_pytest_report.py "${_py_xml}" "[[PYTEST_MARKER]]"
exit "${_rc}"
"""
)


# Turns pytest's JUnit XML (xunit1: carries each case's `file`) into one line per test:
#   <marker> PASS|FAIL|SKIP <file>::<Class>::<name>
# plus `<file>::<file>` per test file -- FAIL when the file did not collect. No XML at
# all (pytest never ran or crashed) is recorded as a failed `<pytest>` entry.
PYTEST_REPORT_PY = r"""import sys
import xml.etree.ElementTree as ET

xml_path, marker = sys.argv[1], sys.argv[2]


def emit(status, name):
    print(f"{marker} {status} {name}")


try:
    root = ET.parse(xml_path).getroot()
except Exception:
    emit("FAIL", "<pytest>::<pytest>")
    sys.exit(0)

files_ok = {}
for case in root.iter("testcase"):
    cls = case.get("classname") or ""
    name = case.get("name") or ""
    path = case.get("file")
    failed = case.find("failure") is not None or case.find("error") is not None
    skipped = case.find("skipped") is not None
    if not path:
        # A collection error: name is the module path in dotted form.
        path = (cls or name).replace(".", "/") + ".py"
        files_ok[path] = False
        continue
    module = path[:-3].replace("/", ".") if path.endswith(".py") else path
    rest = cls[len(module):].lstrip(".") if cls.startswith(module) else ""
    node = "::".join([path] + ([rest.replace(".", "::")] if rest else []) + [name])
    emit("FAIL" if failed else "SKIP" if skipped else "PASS", node)
    files_ok.setdefault(path, True)
for path, ok in sorted(files_ok.items()):
    emit("PASS" if ok else "FAIL", f"{path}::<file>")
"""


class EccImageDefault(Image):
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
        return EccImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @property
    def repo_dir(self) -> str:
        return f"/home/{self.pr.repo}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", self.repo_dir)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[USER]]", TEST_USER)
            .replace("[[PY_VENV]]", PY_VENV)
            .replace("[[PYTEST_MARKER]]", PYTEST_MARKER)
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
            File(".", "ecc_pytest_report.py", PYTEST_REPORT_PY),
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        repo_dir = self.repo_dir
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # PR images are never touched by DockerfileEnhancer (it returns raw the
        # moment dependency() is an Image), so everything this layer needs is
        # written out here. The scrub lives here rather than in the shared base: the
        # base must keep full history so every PR of the dataset can reach its own
        # commit.
        #
        # The scrub deliberately contains no `git reset`, no `git clean` and no
        # path-scoped checkout -- prepare.sh is allowed to have modified tracked
        # files, and any of those would silently revert that work. The opening
        # commit-scoped `--detach` is a same-tree no-op that preserves edits.
        return f"""FROM {name}:{tag}
{self.global_env}
{copies}RUN bash /home/prepare.sh
WORKDIR {repo_dir}
RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# run-all.js section header: one per test file, at column 0.
_FILE_HEADER_RE = re.compile(r"^━━━ Running (\S+\.test\.js) ━━━\s*$")
# run-all.js file-level failure verdict, at column 0. The reason text varies between
# stages (status number, signal, failed-test count), so it is never part of a name.
_FILE_FAIL_RE = re.compile(
    r"^✗ (\S+\.test\.js) (?:exited with status \d+|terminated by signal \S+"
    r"|failed to start: .*|reported \d+ failed tests)\s*$"
)
# The final summary box: every section before it received its file-level verdict.
# Python lane lines (see PYTEST_REPORT_PY); they are printed after the JS summary.
_PYTEST_RE = re.compile(r"^" + re.escape(PYTEST_MARKER) + r" (PASS|FAIL|SKIP) (.+?)\s*$")
_FINAL_RESULTS_RE = re.compile(r"^║\s+Final Results\s+║\s*$")
# Per-test lines inside a section: the custom harness prints `✓/✗ <name>` or
# `PASS/FAIL <name>` (usually indented, not always); node:test prints TAP at column 0.
# A trailing " # SKIP"/" # TODO" marks a skipped TAP test. File-level verdicts and
# the summary box are matched first, so they never reach these patterns.
_PASS_RES = [
    re.compile(r"^\s*[✓✔]\s+(.+?)\s*$"),
    re.compile(r"^\s*PASS\s+(.+?)\s*$"),
]
_FAIL_RES = [
    re.compile(r"^\s*[✗✘❌]\s+(.+?)\s*$"),
    re.compile(r"^\s*FAIL\s+(.+?)\s*$"),
]
_TAP_RE = re.compile(r"^(not ok|ok)\s+\d+(?:\s+-)?\s+(.+?)\s*$")
_TAP_DIRECTIVE_RE = re.compile(r"\s+#\s+(SKIP|TODO)\b.*$", re.I)

# Volatile tokens some test titles embed, which differ on every run of the SAME
# commit and would otherwise split one test into a different name per stage:
#   * epoch-millisecond deadlines (hooks/session-end.test.js titles a case with
#     `Date.now() + 300000`, e.g. "... bounds the deadline 1793955489958");
#   * mkdtemp directories (`/tmp/ecc-stocktake-resume-ohbcVU/...`).
_VOLATILE_RES = [
    (re.compile(r"\b1[5-9]\d{11}\b"), "<epoch-ms>"),
    (re.compile(r"/tmp/([A-Za-z0-9._-]*?-)?[A-Za-z0-9]{6}(?=[/\s:]|$)"), "/tmp/<tmp>"),
]
# Several helpers print a failure as `✗ <name>: <error message>`, while the passing
# line is just `✓ <name>`. The message is cut so a test keeps ONE name whatever its
# outcome; otherwise a PASS->FAIL regression would read as two unrelated tests. Only
# a suffix that looks like an error is cut: legitimate titles contain ": " too
# (`PASS skills/x/SKILL.md: rejects ...`).
_FAIL_MESSAGE_RE = re.compile(
    r":\s+(?:/|[A-Z]\w*Error\b|Error\b|Expected\b|expected\b|Command failed"
    r"|E[A-Z]{3,}\b|A failed|The expression|Missing expected|Cannot |spawn\w*\b"
    r"|.*command not found).*$"
)


def _name(path: str, title: str, failed: bool = False) -> str:
    if failed:
        title = _FAIL_MESSAGE_RE.sub("", title)
    for rx, repl in _VOLATILE_RES:
        title = rx.sub(repl, title)
    return f"{path}::{title.strip()}"


def parse_ecc_log(test_log: str) -> TestResult:
    """Parse `node tests/run-all.js` output.

    Names are `tests/<file>::<test title>`: run-all.js runs every file separately
    and the same title can appear in more than one file, so the repo-relative file
    path keeps names unique (and lets the harness match them to test-patch paths).
    Each file also gets `tests/<file>::<file>`, its own verdict. Volatile tokens and
    failure messages are normalised out of titles (see above). Within one file a
    title that occurs twice collapses to one name -- an occurrence counter would
    shift whenever a patch adds a same-titled case earlier in the file -- and if
    either occurrence failed, the name is failed.
    """
    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    seen_files: list[str] = []
    failed_files: set[str] = set()
    finished = False
    current: Optional[str] = None

    for raw in _ANSI_RE.sub("", test_log).splitlines():
        line = raw.rstrip("\r")
        m = _FILE_HEADER_RE.match(line)
        if m:
            current = "tests/" + m.group(1)
            seen_files.append(current)
            continue
        m = _FILE_FAIL_RE.match(line)
        if m:
            failed_files.add("tests/" + m.group(1))
            continue
        if _FINAL_RESULTS_RE.match(line):
            finished = True
            current = None
            continue
        m = _PYTEST_RE.match(line)
        if m:
            status, node = m.group(1), m.group(2)
            path, _, title = node.partition("::")
            if status == "PASS":
                passed.add(_name(path, title))
            elif status == "SKIP":
                skipped.add(_name(path, title))
            else:
                failed.add(_name(path, title, failed=True))
            continue
        if current is None:
            continue

        m = _TAP_RE.match(line)
        if m:
            status, name = m.group(1), m.group(2)
            if _TAP_DIRECTIVE_RE.search(name):
                skipped.add(_name(current, _TAP_DIRECTIVE_RE.sub("", name)))
            elif status == "ok":
                passed.add(_name(current, name))
            else:
                failed.add(_name(current, name, failed=True))
            continue
        for rx in _FAIL_RES:
            m = rx.match(line)
            if m:
                failed.add(_name(current, m.group(1), failed=True))
                break
        else:
            for rx in _PASS_RES:
                m = rx.match(line)
                if m:
                    passed.add(_name(current, m.group(1)))
                    break

    # File-level verdicts. A file is passed only when run-all.js reached its final
    # summary without printing a failure verdict for it; a file whose section was cut
    # off (timeout, crash of the runner) is failed, never passed.
    for path in seen_files:
        key = f"{path}::{FILE_TEST_SUFFIX}"
        if path in failed_files or not finished:
            failed.add(key)
        else:
            passed.add(key)
    for path in failed_files - set(seen_files):
        failed.add(f"{path}::{FILE_TEST_SUFFIX}")

    passed -= failed
    skipped -= passed
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


# Two keys, one class. The records carry `repo: "ECC"` and no `number_interval`, so
# Instance.create looks up `affaan-m/ECC` -- that key is what routes this dataset
# and must stay. `ECC_3640_to_3001` (this file's stem, PRs 3640..3001) is the
# interval-style name, for records or tools that address the range explicitly.
@Instance.register("affaan-m", "ECC_3640_to_3001")
@Instance.register("affaan-m", "ECC")
class ECC_3640_TO_3001(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return EccImageDefault(self.pr, self._config)

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
        return parse_ecc_log(test_log)
