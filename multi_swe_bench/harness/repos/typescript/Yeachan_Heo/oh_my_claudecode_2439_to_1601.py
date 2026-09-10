"""Yeachan-Heo/oh-my-claudecode harness for the PR #1601 - #2439 era.

Dataset: ``Yeachan-Heo__oh-my-claudecode_raw_dataset.jsonl`` (20 rows,
#1601 ... #2439, 20 distinct ``base.sha`` values, base branch ``dev``).

Era evidence, read from the repository at the earliest and the latest
``base.sha`` in the dataset (``5f616540`` for #1601 and ``118f3250`` for
#2439)::

    package.json engines      node >=20.0.0                (identical at both)
    package manager           package-lock.json, lockfileVersion 3  -> npm
    .nvmrc / .node-version    absent
    packageManager field      absent
    test script               "vitest"  /  test:run "vitest run"
    vitest (locked)           ^4.0.17
    better-sqlite3 (locked)   ^12.6.2  (guarded dynamic import, not gated)
    vitest.config.ts          include src/**/*.{test,spec}.*,
                              exclude node_modules, dist, .omc; testTimeout 30000

This is the same toolchain the adjacent #1123-#1399 and #786-#1075 shards
target, so the shared-base shape carries over verbatim; only the interval
endpoints and the class/tag names differ.

Architecture (matches the shared-base reference shape):

* ``ImageBase`` is a **single shared** image for the whole era: toolchain plus
  a full-history clone, pinned to nothing. Its Dockerfile opens with the
  BuildKit syntax directive, which is the ``DockerfileEnhancer`` opt-out
  (``image.py``); without it the enhancer would rewrite the clone into
  ``git checkout ${BASE_COMMIT}`` + history scrub and pin an image shared by
  20 PRs to whichever ``base.sha`` won the image dedup. Because the enhancer is
  opted out, this file emits the infrastructure it would have injected (proxy
  ARGs, TLS/CA env, CA symlink farm, OCI labels) itself. Every value it needs
  is read from the PR object (``pr.org``, ``pr.repo``, ``pr.base.sha``,
  ``pr.number``) - nothing PR-specific is hardcoded.
* ``ImageDefault`` (one per PR) owns the checkout and the history prune, so
  every PR reaches its own ``base.sha`` from the same base image.

Repo-specific notes:

* ``dev`` is force-pushed, so a ``base.sha`` can be unreachable from any ref in
  the shared clone. ``prepare.sh`` re-fetches the commit by sha when
  ``git cat-file -e`` cannot find it (GitHub serves every sha by
  ``git fetch --depth 1 origin <sha>``).
* Tests never need a build: vitest transpiles ``src`` directly and its
  ``exclude`` drops ``dist``, so ``prepare.sh`` stops after ``npm ci``.
  ``better-sqlite3`` and ``@ast-grep/napi`` are loaded through guarded dynamic
  imports, so they are not part of the dependency gate.
* The full suite runs in every stage with an identical command; report.py's
  baseline-first classifier derives F2P/P2P from the three stages. Test
  identifiers are ``<repo-relative file> > <describe...> > <test title>``.

Routing: registered under the ``number_interval`` key
``oh_my_claudecode_2439_to_1601`` (and its bundle spelling ``1601-2439``). A
grading run injects that ``number_interval`` into the dataset rows, so this
shard is reached directly without touching the adjacent shards' org/repo
router.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "oh_my_claudecode_2439_to_1601"
_BASE_TAG = "base-2439_to_1601"

# package.json engines is node >=20.0.0 and the locked vitest ^4.0.17 accepts
# ^20.0.0; node:20-bookworm also carries npm 10, which reads the
# lockfileVersion 3 package-lock.json this era ships.
_NODE_IMAGE = "node:20-bookworm"

_VITEST_REPORT = "/home/vitest-report.json"

# --no-file-parallelism removes worker count and file scheduling as a source of
#   cross-stage variance. The two explicit timeouts pin behaviour across the
#   range regardless of the vitest.config.ts testTimeout default.
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

# One suite in this repo names a case after a literal control character, which
# would otherwise travel into the report as a raw NUL. Folding C0/DEL to a space
# is applied identically in every stage, so it cannot make a name drift.
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
    for item in nocases:
        sys.stdout.write("NOCASES %s\n" % item)
    for line in failures[:400]:
        sys.stdout.write(line + "\n")
    sys.stdout.flush()

    if not lines:
        sys.stderr.write("EMIT_ERROR no test cases found in %s\n" % report_path)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


# prepare.sh runs once, at PR-image build time, where the network is available.
# It leaves the tree detached at base.sha with node_modules populated, so the
# three graded stages never install anything and never need the network.
_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

# `dev` is force-pushed upstream, so a base commit can be unreachable from
# every ref the shared base image cloned. GitHub still serves it by sha.
if ! git cat-file -e __BASE_SHA__^{commit} 2>/dev/null; then
    git remote add origin "https://github.com/__ORG__/__REPO__.git" 2>/dev/null || true
    git fetch --quiet --no-tags origin __BASE_SHA__
fi

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

# Deterministic install from the committed lockfile, retried for transient
# network flakes. No swallowing `|| true`: a genuine failure aborts the build
# here (the P14 hard gate below is the backstop, not the decider). better-sqlite3
# is a native dependency, but it ships linux amd64/arm64 prebuilds via
# prebuild-install (with build-essential + python3 as the node-gyp source-build
# fallback), so `npm ci` resolves on both architectures without a swallow.
npm_ci_ok=0
for attempt in 1 2 3; do
    if npm ci --no-audit --no-fund; then npm_ci_ok=1; break; fi
    echo "prepare: npm ci attempt $attempt failed; retrying in 10s"
    sleep 10
done
test "$npm_ci_ok" = 1

# Hard gate: runs last. Backstops the install above by proving the tree the three
# graded stages will actually use is present and runnable.
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


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------

# Shared base. Line 1 is the DockerfileEnhancer opt-out, so everything the
# enhancer would have injected is written out here instead. BASE_COMMIT is
# DECLARED (the harness passes it to every build and BuildKit warns about an
# unused build arg) but never REFERENCED - referencing it is what would pin an
# image that 20 PRs share to a single commit.
_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    python3 \\
    build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null
"""


# Per-PR layer. It owns the pin and the prune because the base is shared.
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


def oh_my_claudecode_parse_log(test_log: str) -> TestResult:
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


class OhMyClaudecodeEraImageBase2439To1601(Image):
    """Shared, unpinned base: toolchain plus a full-history clone."""

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

        body = (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )

        sections = [body.strip()]
        for part in (self.clear_env, 'CMD ["/bin/bash"]'):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"

    def _merged_env_block(self) -> str:
        """One ENV instruction: enhancer block + --global_env + toolchain vars."""
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(
            (
                "CI=true",
                "NODE_ENV=test",
                "NODE_OPTIONS=--max-old-space-size=4096",
                "NPM_CONFIG_AUDIT=false",
                "NPM_CONFIG_FUND=false",
                "NPM_CONFIG_UPDATE_NOTIFIER=false",
                "FORCE_COLOR=0",
                "NO_COLOR=1",
            )
        )
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )


class OhMyClaudecodeEraImageDefault2439To1601(Image):
    """Per-PR layer: stages the patches and scripts, pins and prunes."""

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
        return OhMyClaudecodeEraImageBase2439To1601(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return (
            template.replace("__REPO__", self.pr.repo)
            .replace("__ORG__", self.pr.org)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPORT__", _VITEST_REPORT)
            .replace("__VITEST_FLAGS__", _VITEST_FLAGS)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "emit_testcases.py", _EMIT_TESTCASES_PY),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("The dependency of the default image must be an image.")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        sections = [f"FROM {image.image_full_name()}"]
        for part in (
            self.global_env,
            copy_commands,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            self._render(_PRUNE),
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Instance
# ---------------------------------------------------------------------------


@Instance.register("Yeachan-Heo", _INTERVAL_NAME)
class OH_MY_CLAUDECODE_2439_TO_1601(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeEraImageDefault2439To1601(self.pr, self._config)

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
        return oh_my_claudecode_parse_log(test_log)


# The bundle spelling of this era's number_interval, registered so a JSONL that
# carries `number_interval` in either form resolves to this shard.
Instance.register("Yeachan-Heo", "1601-2439")(OH_MY_CLAUDECODE_2439_TO_1601)
