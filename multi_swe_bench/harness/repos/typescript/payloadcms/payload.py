"""Shared helpers for the payloadcms/payload era configs.

This module registers no Instance. It holds the pieces both eras need: the log
parser, the Dockerfile builders, and the container-side shell scripts.

Script and Dockerfile templates use ``@@TOKEN@@`` placeholders substituted by
`render()` rather than ``str.format``: the bodies are bash and contain
``${...}``, ``$(...)`` and ``^{commit}``, all of which would have to be
brace-escaped under ``format`` — the exact kind of edit that silently corrupts
a script.

Image layering follows the reference Dockerfiles:

  base image  — infrastructure header, toolchain, MongoDB, and a *plain clone*.
                No checkout, no history hardening: one base image is shared by
                every PR in the era, so pinning it to a commit here would prune
                every other PR's base commit out of the object store.
  PR image    — fetches this PR's base commit if the clone does not already
                carry it, checks it out, hardens the history down to it, then
                installs dependencies.
"""

import re

from multi_swe_bench.harness.image import DockerfileEnhancer
from multi_swe_bench.harness.instance import TestResult

# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def render(template: str, **tokens: object) -> str:
    """Substitute ``@@NAME@@`` placeholders. Raises if any are left over."""
    out = template
    for key, value in tokens.items():
        out = out.replace(f"@@{key.upper()}@@", str(value))
    leftover = re.findall(r"@@[A-Z_]+@@", out)
    if leftover:
        raise ValueError(f"unsubstituted template tokens: {sorted(set(leftover))}")
    return out


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
#
# Payload emits two very different test logs into the same stage output:
#
#   Jest (jest.config.js sets `verbose: true` in every era):
#       PASS test/auth/int.spec.ts
#         Auth
#           ✓ should login (50 ms)
#           ✕ should reject (3 ms)
#           ○ skipped should ignore
#
#   Playwright, `list` reporter:
#         ✓  1 [chromium] › test/admin/e2e.spec.ts:12:3 › admin › saves (1.2s)
#         ✘  2 [chromium] › test/admin/e2e.spec.ts:20:3 › admin › fails (5.0s)
#         -  3 [chromium] › test/admin/e2e.spec.ts:28:3 › admin › skipped
#
# Two properties matter more than anything else here:
#
#   uniqueness  — a bare `it()` name is not unique across a repo this size, so
#                 Jest names are qualified with the file from the PASS/FAIL
#                 header that precedes them. Playwright names already carry
#                 file:line:col and the describe chain.
#   stability   — the same test must yield the same string in all three stages.
#                 Everything variable is stripped: durations, `(N tests)`
#                 counts, `(retry #1)` markers, and the Playwright list
#                 reporter's ordinal, which shifts whenever test.patch adds a
#                 test ahead of it.

_MARK_PASS = "✔✓√"
_MARK_FAIL = "×✕✗✘✖"
_MARK_SKIP = "○◌"
_MARK_ALL = _MARK_PASS + _MARK_FAIL + _MARK_SKIP

# Full CSI, not just SGR: Jest and Playwright emit erase-line (\x1b[2K) and
# cursor-column (\x1b[0G) codes that would otherwise survive and break the ^ anchor.
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# "PASS test/auth/int.spec.ts" / "FAIL packages/x/y.spec.ts (12.3 s)"
_JEST_FILE_RE = re.compile(r"^(?:PASS|FAIL)\s+(?P<file>[^\s(]+\.[cm]?[jt]sx?)\b")

# Playwright list reporter: mark, ordinal, then the test id. The "[project]"
# prefix is optional — it only appears when playwright.config.ts declares
# `projects`, and payload's does not, so real output is
# "✘  1 uploads/e2e.spec.ts:58:7 › uploads › should see upload filename".
# The `›` is what separates these from a Jest leaf whose name starts with a digit.
_PLAYWRIGHT_RE = re.compile(
    r"^(?P<mark>[" + _MARK_ALL + r"-])\s+\d+\s+(?P<name>(?:\[[^\]]+\]\s*)?\S.*›.+)$"
)

# Jest / Vitest per-test line.
_UNIT_RE = re.compile(
    r"^(?P<mark>[" + _MARK_ALL + r"])\s+(?:skipped\s+)?(?P<name>\S.*?)$"
)

# Explicit "[PASS] name" form, kept for harness-emitted summaries.
_BRACKET_RE = re.compile(r"^\[(?P<mark>PASS|FAIL|SKIP)\]:?\s+(?P<name>\S.*?)$")

# Variable trailing metadata, stripped repeatedly until the name is stable.
_TRAILING_RES = [
    re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|m|min)\)$"),  # "(1.2s)" "(150 ms)"
    re.compile(r"\s+\d+(?:\.\d+)?\s*(?:ms|s)$"),  # vitest "12ms"
    re.compile(r"\s*\(\d+\s+tests?(?:\s*\|[^)]*)?\)$"),  # vitest "(3 tests)"
    re.compile(r"\s*\(retry\s*#?\s*\d+\)$", re.IGNORECASE),  # playwright retries
    re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\s*\)$"),
]


def _strip_trailing_metadata(name: str) -> str:
    previous = None
    while previous != name:
        previous = name
        for pattern in _TRAILING_RES:
            name = pattern.sub("", name)
        name = name.rstrip()
    return name


def _normalize(test_log: str) -> list[str]:
    text = _OSC_RE.sub("", test_log)
    text = _CSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n")


def payload_parse_log(test_log: str) -> TestResult:
    """Parse a combined Jest + Playwright stage log into a TestResult."""
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    current_file = ""

    def classify(mark: str, name: str) -> None:
        name = _strip_trailing_metadata(name)
        if not name:
            return
        if mark in _MARK_PASS or mark == "PASS":
            passed_tests.add(name)
        elif mark in _MARK_FAIL or mark == "FAIL":
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    for raw_line in _normalize(test_log):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        header = _JEST_FILE_RE.match(line.strip())
        if header:
            current_file = header.group("file")
            continue

        stripped = line.strip()

        match = _PLAYWRIGHT_RE.match(stripped)
        if match:
            # Already fully qualified (project, file:line:col, describe chain).
            classify(match.group("mark"), match.group("name"))
            continue

        match = _BRACKET_RE.match(stripped)
        if match:
            classify(match.group("mark"), match.group("name"))
            continue

        match = _UNIT_RE.match(stripped)
        if match:
            name = match.group("name")
            # A Vitest file-level line already names the file; a Jest leaf does
            # not, so qualify it with the enclosing PASS/FAIL header.
            looks_like_path = bool(re.match(r"^[\w./@-]+\.[cm]?[jt]sx?\b", name))
            if current_file and not looks_like_path:
                name = f"{current_file} › {name}"
            classify(match.group("mark"), name)
            continue

    # TestResult requires the three sets to be pairwise disjoint and the counts
    # to equal the set sizes. Precedence: failed > passed > skipped, so a test
    # that failed on one shard and passed on a retry is recorded as failed.
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


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------

_BASE_DOCKERFILE = """@@HEADER@@

WORKDIR /home/

@@SETUP@@

RUN git clone "${REPO_URL}" /home/@@REPO@@

CMD ["/bin/bash"]
"""


def base_dockerfile(image, base_img: str, setup: str) -> str:
    """Infrastructure header + toolchain + a plain clone.

    The BuildKit syntax directive is emitted here on purpose. DockerfileEnhancer
    short-circuits on it (`if cls.SYNTAX_DIRECTIVE in raw: return raw`), which is
    what keeps this a clone-only image. Left to the enhancer, it would append
    `git checkout ${BASE_COMMIT}` plus the history-hardening block; since one
    base image is shared by every PR in the era, that would prune every other
    PR's base commit out of the object store. Pinning belongs in the PR image.

    The header itself is produced by the enhancer's own builder, so the proxy
    ARGs, ENV block, OCI labels and CA-bundle symlinks cannot drift from it.
    """
    header = (
        f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}\n\n"
        f"FROM {base_img}\n\n"
        f"{DockerfileEnhancer._infrastructure_block(image, base_img)}"
    )
    return render(
        _BASE_DOCKERFILE,
        header=header.rstrip(),
        setup=setup.strip(),
        repo=image.pr.repo,
    )


_PR_DOCKERFILE = """FROM @@BASE_IMAGE@@

@@COPIES@@
WORKDIR /home/@@REPO@@

# prepare.sh recovers this PR's base commit if the shared base image's clone does
# not carry it, checks it out, and installs. It has to precede the hardening block
# below, which asserts HEAD is that commit and prunes everything else away.
RUN bash /home/prepare.sh

# Git stripping / hardening. Pins the tree to the base commit and reduces the
# repository to exactly that history, then asserts the four invariants:
# HEAD == base commit, no residual refs, no remotes, no unreachable objects.
RUN set -eux; \\
    git checkout --detach @@SHA@@; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse @@SHA@@)"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


def pr_dockerfile(image) -> str:
    """Per-PR image: pin to this PR's base commit, harden, then install."""
    base = image.dependency()
    copies = "".join(f"COPY {file.name} /home/\n" for file in image.files())
    return render(
        _PR_DOCKERFILE,
        base_image=base.image_full_name(),
        copies=copies,
        repo=image.pr.repo,
        sha=image.pr.base.sha,
        number=image.pr.number,
    )


# Base-commit recovery, injected into each era's prepare.sh.
#
# The shared base image is a plain clone, so in practice it already carries every
# PR's base commit and the `cat-file` guard short-circuits. The fetches are the
# fallback for a commit GitHub still serves but the clone cannot see — one whose
# branch was deleted, or force-pushed away. All three failing is a hard error:
# without the commit there is nothing to grade, so `set -e` stops the build here
# rather than letting the hardening block fail with a less obvious message.
_RECOVER_BASE_COMMIT = r"""git cat-file -e @@SHA@@^{commit} 2>/dev/null \
  || git fetch --no-tags --depth=2147483647 origin @@SHA@@ \
  || git fetch --no-tags origin "+refs/pull/@@NUMBER@@/head:refs/remotes/origin/pr-@@NUMBER@@"

git checkout --detach @@SHA@@
"""


def recover_base_commit(pr) -> str:
    return render(_RECOVER_BASE_COMMIT, sha=pr.base.sha, number=pr.number)


# ---------------------------------------------------------------------------
# Container-side helper scripts
# ---------------------------------------------------------------------------

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: not inside a git repository" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: uncommitted changes" >&2
  git status --porcelain >&2
  exit 1
fi

echo "check_git_changes: clean"
"""


STRIP_BINARY_DIFFS_PY = r'''#!/usr/bin/env python3
"""Drop binary file diffs from a patch so `git apply` does not reject the whole patch.

payloadcms test patches carry image fixtures (PR 1835 adds test/fields/uploads/payload.png,
PR 2000 adds test/fields/collections/Uploads3/payload.jpg). Those hunks are irrelevant to the
verdict but fatal to `git apply`, so they are removed before the patch is applied.
"""
import re
import sys


def strip_binary_diffs(patch_path):
    with open(patch_path, "r", errors="replace") as handle:
        content = handle.read()

    diffs = re.split(r"(?=^diff --git )", content, flags=re.MULTILINE)
    kept = []
    for diff in diffs:
        if not diff.strip():
            continue
        if "GIT binary patch" in diff or "Binary files" in diff:
            sys.stderr.write("strip_binary_diffs: dropped binary hunk\n")
            continue
        kept.append(diff)

    with open(patch_path, "w") as handle:
        handle.write("".join(kept))


if __name__ == "__main__":
    for path in sys.argv[1:]:
        strip_binary_diffs(path)
'''


# Payload connects to the MongoDB default port with no authentication:
#   demo/server.ts                    mongodb://localhost/payload
#   test/helpers/configHelpers.ts     mongodb://localhost/<uuid>
#   test/buildConfigWithDefaults.ts   mongodb://127.0.0.1/payloadtests
# so mongod must listen on 27017 without auth. A single-node replica set is used
# because the payload mongoose adapter opens transactions when one is available.
START_MONGO_SH = r"""#!/bin/bash
set -eo pipefail

MONGO_PORT=27017
MONGO_DB_PATH=/data/db
MONGO_LOG=/var/log/mongod.log

mkdir -p "$MONGO_DB_PATH"

if mongosh --quiet --port "$MONGO_PORT" --eval 'db.runCommand({ping:1})' >/dev/null 2>&1; then
  echo "start-mongo: already running on $MONGO_PORT"
  exit 0
fi

mongod --replSet rs0 --port "$MONGO_PORT" --dbpath "$MONGO_DB_PATH" \
       --bind_ip 127.0.0.1 --fork --logpath "$MONGO_LOG"

for _ in $(seq 1 60); do
  if mongosh --quiet --port "$MONGO_PORT" --eval 'db.runCommand({ping:1})' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

mongosh --quiet --port "$MONGO_PORT" --eval '
try { rs.initiate({_id: "rs0", members: [{_id: 0, host: "127.0.0.1:27017"}]}); }
catch (e) { print("rs.initiate: " + e); }
' >/dev/null 2>&1 || true

for _ in $(seq 1 60); do
  if mongosh --quiet --port "$MONGO_PORT" --eval 'db.hello().isWritablePrimary' 2>/dev/null | grep -q true; then
    echo "start-mongo: primary ready on $MONGO_PORT"
    exit 0
  fi
  sleep 1
done

echo "start-mongo: mongod did not reach PRIMARY on $MONGO_PORT" >&2
tail -n 40 "$MONGO_LOG" >&2 || true
exit 1
"""


# Which tests a stage runs is derived from test.patch rather than fixed, because
# running payload's whole integration suite three times per PR is hours of work
# for tests that cannot move between stages. What *can* move is what the patch
# touches, so that is what gets run:
#
#   * every non-e2e spec file the patch edits or adds, and
#   * the int.spec.ts of every test/<suite>/ directory the patch touches — a
#     patch that only changes test/uploads/config.ts still changes what
#     test/uploads/int.spec.ts observes.
#
# Only paths that exist in the current tree are emitted, so the RUN stage
# (before test.patch is applied) simply reports fewer targets.
# Both selection scripts end in an explicit `exit 0`, and every internal
# pipeline is `|| true`-guarded. Selecting nothing is a normal outcome — an
# added spec simply does not exist yet in the RUN stage — but a bare `grep`
# that matches nothing exits 1, and so does a trailing `[ -f "$f" ] && ...`
# AND-list whose test fails. Either would propagate out of the command
# substitution in the stage script and, under `set -e`, kill the stage
# silently right after the patch step.
SELECT_TARGETS_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/@@REPO@@

TOUCHED=$(grep -E '^diff --git a/' /home/test.patch \
          | sed 's|^diff --git a/||; s| b/.*$||' | sort -u || true)

CANDIDATES=$(
  {
    printf '%s\n' "$TOUCHED" | grep -E '\.spec\.(ts|js)$' | grep -v 'e2e\.spec\.' || true
    printf '%s\n' "$TOUCHED" | grep -E '^test/[^/]+/' \
      | sed -E 's|^(test/[^/]+)/.*|\1/int.spec.ts|' || true
  } | sort -u
)

for f in $CANDIDATES; do
  if [ -n "$f" ] && [ -f "$f" ]; then
    printf '%s\n' "$f"
  fi
done

exit 0
"""


SELECT_E2E_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/@@REPO@@

CANDIDATES=$(grep -E '^diff --git a/.*e2e\.spec\.ts' /home/test.patch \
             | sed 's|^diff --git a/||; s| b/.*$||' | sort -u || true)

for f in $CANDIDATES; do
  if [ -n "$f" ] && [ -f "$f" ]; then
    printf '%s\n' "$f"
  fi
done

exit 0
"""


# The e2e specs boot payload in-process (test/helpers/configHelpers.ts::initPayloadE2E),
# so no dev server is started here. playwright.config.ts lives at the REPO ROOT in every
# era (test/ holds only the specs). `--reporter=list` is mandatory: with CI=true set,
# Playwright's default reporter is `dot`, which prints no test names at all.
# `--workers=1` overrides the repo config's `workers: 999`, which is unusable in a
# memory-capped container and makes results nondeterministic.
RUN_E2E_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/@@REPO@@

SPECS=$(bash /home/select-e2e.sh)

if [ -z "$SPECS" ]; then
  echo "run-e2e: no e2e spec from the test patch is present in this tree"
  exit 0
fi

CONFIG=playwright.config.ts
if [ ! -f "$CONFIG" ]; then
  echo "run-e2e: $CONFIG not found at repo root; skipping e2e"
  exit 0
fi

if [ ! -x node_modules/.bin/playwright ]; then
  echo "run-e2e: playwright is not installed in this tree; skipping e2e"
  exit 0
fi

status=0
for spec in $SPECS; do
  echo "run-e2e: === $spec ==="
  rm -rf node_modules/.cache/webpack
  # --timeout caps a single test. payload's config sets 180s, which turns a
  # suite that cannot reach its dev server into 3 minutes of dead wall-clock per
  # test; a genuine payload e2e test finishes in seconds.
  timeout --preserve-status "$E2E_TIMEOUT" \
    node_modules/.bin/playwright test "$spec" -c "$CONFIG" \
      --workers=1 --reporter=list --timeout=60000 || status=$?
done

exit $status
"""


# ---------------------------------------------------------------------------
# Run scripts (RUN / TEST / FIX)
# ---------------------------------------------------------------------------
#
# All three stages are generated from one template with only the patch step
# differing, so the test command is identical across stages by construction —
# a divergence there would make the f2p comparison meaningless.
#
# `|| true` never guards a test command. Test *failure* is expected in the RUN
# and TEST stages, so the runner's exit status is tolerated explicitly via
# `if ! ...; then`; a runner that fails to *start* is caught by the output
# assertion, which is what `|| true` would otherwise hide. The assertion is
# skipped only when the stage genuinely had nothing to run — a distinct and
# legitimate state, reported explicitly rather than silently.

_STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export DISABLE_LOGGING=true
export NODE_NO_WARNINGS=1
export NODE_OPTIONS="--no-deprecation --max-old-space-size=4096"
export JEST_TIMEOUT=1200
export E2E_TIMEOUT=900

# payload's test helpers (test/helpers/configHelpers.ts) boot a
# mongodb-memory-server, which by default downloads a mongod tarball from
# fastdl.mongodb.org. No linux-aarch64 build is published for the version it
# asks for, so on arm64 that download 403s and every suite fails before it
# starts. Pointing it at the mongod already installed in the image removes the
# download entirely — and makes the stage independent of that host besides.
export MONGOMS_SYSTEM_BINARY=/usr/bin/mongod
export MONGOMS_STORAGE_ENGINE=wiredTiger

# A real mongod on the default port as well: the pre-`test/` era boots the demo
# server, which connects to mongodb://localhost/payload rather than to a
# memory server.
bash /home/start-mongo.sh

cd /home/@@REPO@@

@@PATCH_STEP@@
STAGE_LOG=/home/stage-output.log
: > "$STAGE_LOG"
RAN_TESTS=0

TARGETS=$(bash /home/select-targets.sh 2>/dev/null | tr '\n' ' ' || true)
if [ -n "$(printf '%s' "$TARGETS" | tr -d '[:space:]')" ]; then
  RAN_TESTS=1
  echo "=== integration tests: @@PM@@ test:int $TARGETS ==="
  if timeout --preserve-status "$JEST_TIMEOUT" @@PM@@ test:int $TARGETS 2>&1 | tee -a "$STAGE_LOG"; then
    echo "=== test:int runner exited 0 ==="
  else
    echo "=== test:int runner exited non-zero (test failures are expected in this stage) ==="
  fi
else
  echo "=== integration tests: no target spec from the test patch exists at this commit ==="
fi

if [ -n "$(bash /home/select-e2e.sh 2>/dev/null || true)" ]; then
  RAN_TESTS=1
  echo "=== e2e tests ==="
  if bash /home/run-e2e.sh 2>&1 | tee -a "$STAGE_LOG"; then
    echo "=== e2e runner exited 0 ==="
  else
    echo "=== e2e runner exited non-zero (test failures are expected in this stage) ==="
  fi
else
  echo "=== e2e tests: no e2e spec from the test patch exists at this commit ==="
fi

# Catch a runner that never started. Without this, a missing binary or a broken
# config would look identical to "every test passed" and parse_log would return
# an empty TestResult, which Report.check() rejects with a useless message.
if [ "$RAN_TESTS" = "1" ]; then
  if ! grep -qE "(^|[[:space:]])(PASS|FAIL|[✔✓√×✕✗✘✖○◌])" "$STAGE_LOG"; then
    echo "FATAL: tests were selected but no recognizable test output was produced" >&2
    tail -n 60 "$STAGE_LOG" >&2 || true
    exit 1
  fi
else
  echo "=== stage ran no tests: nothing the test patch touches exists at this commit ==="
fi

echo "=== Test run complete ==="
"""

_APPLY_TEST_PATCH = r"""python3 /home/strip_binary_diffs.py /home/test.patch
git apply --whitespace=nowarn --verbose /home/test.patch
"""

_APPLY_BOTH_PATCHES = r"""python3 /home/strip_binary_diffs.py /home/test.patch /home/fix.patch
git apply --whitespace=nowarn --verbose /home/test.patch /home/fix.patch
"""


def stage_script(repo: str, package_manager: str, patch_step: str) -> str:
    return render(_STAGE_SH, repo=repo, pm=package_manager, patch_step=patch_step)


def run_sh(repo: str, package_manager: str) -> str:
    """RUN stage — baseline, no patches applied."""
    return stage_script(repo, package_manager, "# RUN stage: no patches applied\n")


def test_run_sh(repo: str, package_manager: str) -> str:
    """TEST stage — test.patch only."""
    return stage_script(repo, package_manager, _APPLY_TEST_PATCH)


def fix_run_sh(repo: str, package_manager: str) -> str:
    """FIX stage — test.patch then fix.patch, in that order."""
    return stage_script(repo, package_manager, _APPLY_BOTH_PATCHES)


def run_e2e_sh(repo: str) -> str:
    return render(RUN_E2E_SH, repo=repo)


def select_targets_sh(repo: str) -> str:
    return render(SELECT_TARGETS_SH, repo=repo)


def select_e2e_sh(repo: str) -> str:
    return render(SELECT_E2E_SH, repo=repo)
