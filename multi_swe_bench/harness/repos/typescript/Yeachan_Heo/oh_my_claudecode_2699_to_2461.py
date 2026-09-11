"""Yeachan-Heo/oh-my-claudecode harness for the PR #2461 - #2699 era.

Dataset: ``Yeachan-Heo__oh-my-claudecode_raw_dataset.jsonl`` (10 rows, #2461 ...
#2699, 7 distinct ``base.sha`` values, base branch ``dev``, all merged between
2026-04-10 and 2026-04-17).

Era evidence, read from the repository at the earliest and the latest
``base.sha`` in the dataset (``3ba840d2`` for #2461/#2462 and ``e9856ebc`` for
#2694/#2698/#2699)::

    package.json engines      node >=20.0.0                (identical at both)
    package manager           package-lock.json, lockfileVersion 3  -> npm
    .nvmrc / .node-version    absent                       (identical at both)
    packageManager field      absent                       (identical at both)
    test script               "vitest"  /  test:run "vitest run"
    vitest (locked)           4.0.18, engines ^20 || ^22 || >=24
    better-sqlite3 (locked)   12.6.2
    vitest.config.ts          include src/**/*.{test,spec}.*,
                              exclude node_modules, dist, .omc,
                              testTimeout 30000, environment node

Nothing in the toolchain moves inside the range, and nothing moves between this
range and the #1123 - #1399 era either: every value above is the same one that
era file records.

Architecture:

* ``ImageBase`` is a **shared** image for this era, tagged ``base-2699_to_2461``:
  toolchain plus one full-history clone, pinned to nothing. Its Dockerfile opens
  with the BuildKit syntax directive, which is the ``DockerfileEnhancer``
  opt-out (``image.py:317``); without it the enhancer would rewrite the clone
  into ``git checkout ${BASE_COMMIT}`` + history scrub and pin an image shared by
  10 PRs to whichever ``base.sha`` won the image dedup. Because the enhancer is
  opted out, this file emits the infrastructure it would have injected (proxy
  ARGs, TLS/CA env, CA symlink farm, OCI labels) itself.

  This era defines its own base rather than importing the #1123 - #1399 one,
  even though the toolchain is identical. Two reasons, and neither is about the
  toolchain. The delivered Dockerfile has to match a fixed expected shape - a
  separate ENV block for the language settings after the CA symlinks, WORKDIR
  before the apt layer, a bare clone line - and that shape differs from the one
  the sibling emits; changing the sibling to match would rewrite the base image
  of an era that has already been processed. Defining it here also removes a
  cross-file coupling that would break this config the next time a name moves
  over there.

* ``ImageDefault`` (one per PR) owns the checkout and the history prune, so
  every PR reaches its own ``base.sha`` from the shared base.

Repo-specific notes:

* ``dev`` is force-pushed, so a ``base.sha`` can be unreachable from any ref in
  the shared clone. ``prepare.sh`` re-fetches the commit by sha when
  ``git cat-file -e`` cannot find it. All 7 shas in this range were verified to
  be present in a fresh clone.
* No patch in this dataset carries a binary hunk (no ``GIT binary patch`` and no
  ``Binary files ... differ`` marker in any of the 20 patch bodies), so no
  ``git apply --exclude`` handling is needed.
* Tests never need a build: vitest transpiles ``src`` directly and its
  ``exclude`` drops ``dist``, so ``prepare.sh`` stops after ``npm ci``.

* **#2461 is a dead row.** Verified against a real clone at every row's
  ``base.sha``: ``git apply --check test.patch`` succeeds for all 10, and
  ``git apply --check test.patch fix.patch`` succeeds for 9. #2461's fix patch
  fails on ``src/hooks/persistent-mode/index.ts``, and the reason is a blob
  mismatch rather than a conflict - the patch names pre-image ``52074cefc``
  while the recorded base commit ``3ba840d2`` holds ``8598d5b13``, and that
  pre-image is not reachable from any ref in the repository. Its other two
  files (``scripts/persistent-mode.mjs``, ``templates/hooks/persistent-mode.mjs``)
  apply cleanly on their own.

  The failing file is production source that both of #2461's test files
  exercise, so dropping the hunk would not rescue the row - it would grade a
  fix with its substance removed. The harness will report #2461 invalid with an
  empty fix stage. That is a dataset defect, not something a config can repair.

Test identifiers are ``<repo-relative file> > <describe...> > <test title>``,
which is the shape ``report.py::_test_name_matches_files`` matches against the
patch file list, and they carry no timing or count metadata, so a name is
stable between stages.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "oh-my-claudecode_2699_to_2461"
_BASE_TAG = "base-2699_to_2461"

# package.json engines is node >=20.0.0 and the locked vitest 4.0.18 accepts
# ^20.0.0; node:20-bookworm also carries npm 10, which reads the
# lockfileVersion 3 package-lock.json this era ships.
_NODE_IMAGE = "node:20-bookworm"

_VITEST_REPORT = "/home/vitest-report.json"

# --no-file-parallelism removes worker count and file scheduling as a source of
#   cross-stage variance, which matters more than the wall clock it costs: a
#   name that moves between stages manufactures a transition that never
#   happened.
# The two explicit timeouts pin behaviour rather than inheriting it. Every
#   commit in this range sets testTimeout 30000 in vitest.config.ts; passing
#   both flags means the graded command does not change if a later commit in
#   the range drops or edits that default.
_VITEST_FLAGS = "--no-file-parallelism --testTimeout=60000 --hookTimeout=60000"


# ---------------------------------------------------------------------------
# emit_testcases.py - vitest JSON report -> TESTCASE lines
# ---------------------------------------------------------------------------

_EMIT_TESTCASES_PY = r'''"""Turn a vitest JSON report into the TESTCASE lines parse_log consumes.

Only ``TESTCASE <STATUS> <identifier>`` lines are parsed by the harness; every
other line printed here is diagnostics for a human reading the stage log. The
identifier is ``<repo-relative file> > <describe...> > <test title>``, which is
the shape report.py's ``_test_name_matches_files`` expects for a JS/TS suite.
"""

import json
import os
import sys

# A test title can carry a literal control character, which would otherwise
# travel into the report as a raw NUL. Folding C0/DEL to a space is applied
# identically in every stage, so it cannot make a name drift between stages.
_CONTROL = dict.fromkeys(range(32), " ")
_CONTROL[127] = " "


def _flatten(text):
    return " ".join(str(text).translate(_CONTROL).split())


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: emit_testcases.py <vitest-report.json> <repo-root>\n")
        return 2

    report_path, root = sys.argv[1], sys.argv[2]
    try:
        with open(report_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        sys.stderr.write("EMIT_ERROR unreadable report %s: %s\n" % (report_path, exc))
        return 1

    # Both spellings of the root are stripped: a symlinked mount would make
    # vitest report the resolved path while the caller passes the logical one.
    prefixes = []
    for candidate in (root, os.path.realpath(root)):
        candidate = candidate.rstrip("/") + "/"
        if candidate not in prefixes:
            prefixes.append(candidate)

    suites = data.get("testResults") or []
    lines = []
    failures = []
    nocases = []

    for suite in suites:
        name = (suite.get("name") or "").replace("\\", "/")
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        while name.startswith("./"):
            name = name[2:]

        cases = suite.get("assertionResults") or []
        if not cases:
            # A suite that fails to import reports no cases. Keeping it visible
            # matters: it is the difference between "this file has no tests"
            # and "this file could not be loaded".
            nocases.append(
                "%s :: %s" % (name, _flatten(suite.get("message") or "")[:300])
            )
            continue

        for case in cases:
            title = _flatten(case.get("title") or "")
            if not title:
                continue
            parts = [name]
            for ancestor in case.get("ancestorTitles") or []:
                ancestor = _flatten(ancestor)
                if ancestor:
                    parts.append(ancestor)
            parts.append(title)
            identifier = " > ".join(parts)

            status = (case.get("status") or "").strip().lower()
            if status in ("failed", "error"):
                label = "FAILED"
                for message in (case.get("failureMessages") or [])[:1]:
                    failures.append(
                        "FAILURE %s :: %s" % (identifier, _flatten(message)[:300])
                    )
            elif status in ("skipped", "pending", "todo", "disabled"):
                label = "SKIPPED"
            else:
                label = "PASSED"
            lines.append("TESTCASE %s %s" % (label, identifier))

    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(
        "STAGE_SUMMARY suites=%d testcases=%d nocases=%d\n"
        % (len(suites), len(lines), len(nocases))
    )
    for entry in nocases:
        sys.stdout.write("NOCASES %s\n" % entry)
    for entry in failures:
        sys.stdout.write("%s\n" % entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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
"""


# prepare.sh runs once, at PR-image build time, where the network is available.
# It leaves the tree detached at base.sha with node_modules populated, so the
# three graded stages never install anything and never need the network.
#
# The clean-tree assertions bracket the checkout only. Nothing after the install
# may assert a clean tree: `npm ci` can rewrite package-lock.json, and the prune
# block in the Dockerfile runs afterwards.
_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false

# emit_testcases.py is written from here rather than shipped as its own File() entry,
# because the PR image directory must contain exactly the eight canonical files (the two
# patches, check_git_changes.sh, prepare.sh and the three stage scripts, plus the
# Dockerfile) and a ninth would make this repo the odd one out. Nothing outside the graded
# stages ever runs it, so a build-time heredoc is the right home for it.
#
# The delimiter is quoted, so the shell expands nothing inside: the script is written to
# disk byte-for-byte as it appears in the config.
cat > /home/emit_testcases.py <<'EMIT_TESTCASES_PY_EOF'
__EMIT_TESTCASES_PY__
EMIT_TESTCASES_PY_EOF

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# `dev` is force-pushed upstream, so a base commit can be unreachable from every
# ref the shared base image cloned. GitHub still serves it by sha.
if ! git cat-file -e __BASE_SHA__^{commit} 2>/dev/null; then
    git remote add origin "https://github.com/__ORG__/__REPO__.git" 2>/dev/null || true
    git fetch --quiet --no-tags origin __BASE_SHA__
fi

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

# `|| true` on the install itself is required (a native optional dependency can
# fail to build on one arch and still leave a usable tree); the hard gate below
# is what decides whether the image is actually usable.
npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

# Hard gate: no `|| true`, runs last. A swallowed install failure would
# otherwise surface three stages later as an empty report that reads as
# "0 failures" instead of "broken image".
test -d node_modules
test -x node_modules/.bin/vitest
npx --no-install vitest --version
node -e "require('./package.json'); console.log('DEPS_OK')"
python3 -c "import json, sys; print('EMIT_TOOLCHAIN_OK')"
"""


# Identical header in all three graded scripts: same env, same cwd.
_SCRIPT_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export FORCE_COLOR=0
export NO_COLOR=1
export TZ=UTC

cd /home/__REPO__
"""


_APPLY_TEST_PATCH = """
git apply --whitespace=nowarn /home/test.patch"""


_APPLY_BOTH_PATCHES = """
git apply --whitespace=nowarn /home/test.patch /home/fix.patch"""


# Concatenated verbatim into run.sh, test-run.sh and fix-run.sh, so the graded
# command is byte-identical in every stage and only the patch application above
# it differs.
#
# `|| STATUS=$?` is on the test runner only: a non-zero exit is the EXPECTED
# outcome of the test stage. It is not a swallowed failure - the `test -s` below
# is a hard gate that aborts the script if vitest never produced a report
# (missing binary, config error, crash on startup), and the runner's status is
# re-raised as the script's own exit status.
_EXEC_TESTS = """
rm -f __REPORT__

STATUS=0
npx --no-install vitest run \\
    --reporter=json \\
    --outputFile=__REPORT__ \\
    __VITEST_FLAGS__ || STATUS=$?

test -s __REPORT__

python3 /home/emit_testcases.py __REPORT__ /home/__REPO__

exit $STATUS
"""


_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _APPLY_TEST_PATCH + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _EXEC_TESTS


# Shared base for this era. Line 1 is the DockerfileEnhancer opt-out
# (image.py:317 returns the content untouched once the syntax directive is
# present), so everything the enhancer would have injected is written out here
# instead: the ARGs, the proxy and TLS env, the OCI labels and the CA symlink
# farm the evaluation harness's MITM proxy needs.
#
# BASE_COMMIT is DECLARED because the harness passes it to every build and
# BuildKit warns about an unused build arg, but it is never REFERENCED.
# Referencing it is exactly what would pin an image shared by 10 PRs to
# whichever base.sha won the image dedup, and every other PR's checkout would
# then fail.
#
# Nothing after the clone. No checkout, no npm install, no hardening - all of
# those are facts about one PR and live in the per-PR image.
_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

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
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

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

ENV CI=true \\
    NODE_ENV=test \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    FORCE_COLOR=0 \\
    NO_COLOR=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

CMD ["/bin/bash"]
"""


# Per-PR prune. It owns the pin because the base is shared.
#
# The prune opens with a COMMIT-scoped `git checkout --detach`, which is a
# same-tree no-op that preserves prepare.sh's work (node_modules, a rewritten
# package-lock.json) while guaranteeing HEAD is detached before every ref is
# deleted. No `git reset`, no `git clean`, no path-scoped checkout and no
# clean-tree assertion may appear from here on.
_PRUNE = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
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
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
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


# ---------------------------------------------------------------------------
# parse_log
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def oh_my_claudecode_2699_parse_log(test_log: str) -> TestResult:
    """Read the TESTCASE lines emit_testcases.py wrote into the stage log.

    Nothing else in the log is parsed. vitest's own human output is not a
    parsing surface - it truncates long names and rewrites lines in place - so
    the JSON report is the single source and this only reads what was already
    normalised from it.
    """
    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for line in clean_log.split("\n"):
        match = _TESTCASE_RE.match(line)
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if status == "FAILED":
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

    # TestResult.__post_init__ requires the three sets to be pairwise disjoint.
    # A failure outranks everything (a retried test can be reported twice), and
    # a real execution outranks a skip.
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
# Images
# ---------------------------------------------------------------------------


class OhMyClaudecodeEraImageBase2699To2461(Image):
    """Shared base for #2461 - #2699: toolchain plus one full-history clone."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class OhMyClaudecodeEraImageDefault2699To2461(Image):
    """PR image for #2461 - #2699, on this era's shared base."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return OhMyClaudecodeEraImageBase2699To2461(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _fill(self, text: str) -> str:
        """Substitute the per-PR placeholders.

        Placeholders rather than an f-string because every one of these bodies
        is shell or Python that is dense with braces; an f-string would need all
        of them doubled and one missed pair is a runtime error in a generated
        script rather than a syntax error here.
        """
        return (
            text.replace("__EMIT_TESTCASES_PY__", _EMIT_TESTCASES_PY)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPORT__", _VITEST_REPORT)
            .replace("__VITEST_FLAGS__", _VITEST_FLAGS)
        )

    def files(self) -> list[File]:
        """The eight canonical files, and nothing else.

        A PR image directory in this harness holds the two patches,
        check_git_changes.sh, prepare.sh and the three stage scripts - plus the Dockerfile
        the caller writes and the build_image.log the build appends. emit_testcases.py is
        deliberately NOT here: prepare.sh writes it at build time, so this directory keeps
        the same shape as every other repo's.
        """
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._fill(_PREPARE_SH)),
            File(".", "run.sh", self._fill(_RUN_SH)),
            File(".", "test-run.sh", self._fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, str):
            raise ValueError("ImageDefault dependency must be an Image")

        # COPY lines are generated from files(), never hand-listed, so a file
        # added there cannot be left uncopied - which would surface at build
        # time as `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # Order matters: copy -> prepare.sh -> prune. prepare.sh checks out the
        # base commit and installs against it, and the prune then re-detaches at
        # that same sha, deletes every other ref, expires the reflog and asserts
        # the result - so nothing later than the base commit is reachable from
        # inside the image and a graded stage cannot read the real fix out of
        # git history. That pruning also removes the full history this image
        # inherits from the shared base.
        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{self._fill(_PRUNE)}"""


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("Yeachan-Heo", _INTERVAL_NAME)
class OH_MY_CLAUDECODE_2699_TO_2461(Instance):
    """Instance for oh-my-claudecode PRs #2461 - #2699 (April 2026, vitest).

    Registered under the number_interval key only. The plain
    `Yeachan-Heo/oh-my-claudecode` name is deliberately left alone: three files
    in this package already register it (oh_my_claudecode.py,
    oh_my_claudecode_1075_to_786.py and oh_my_claudecode_1399_to_1123.py), and
    `Instance.register` is a bare dict assignment with no duplicate check, so a
    fourth would silently replace whichever of those the import order currently
    leaves standing and break dispatch for PRs already processed there.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeEraImageDefault2699To2461(self.pr, self._config)

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
        return oh_my_claudecode_2699_parse_log(test_log)


# The underscore spelling of this era's interval, registered as an alias so a
# JSONL row that carries either form resolves to the same class. Costs nothing
# and neither key belongs to anything else.
Instance.register("Yeachan-Heo", "oh_my_claudecode_2699_to_2461")(
    OH_MY_CLAUDECODE_2699_TO_2461
)
