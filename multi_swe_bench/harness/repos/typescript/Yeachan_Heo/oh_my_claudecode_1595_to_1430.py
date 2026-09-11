"""Yeachan-Heo/oh-my-claudecode harness for the PR #1430 - #1595 era.

Same toolchain as the #1123-#1399 era (verified at our range's base.sha values):
node >=20.0.0, vitest ^4.0.17, npm (package-lock.json lockfileVersion 3), base
branch ``dev``. Shared, unpinned base (clone-only + BuildKit syntax opt-out so
DockerfileEnhancer does not rewrite the clone into a pinned checkout); each PR
owns its own checkout + history prune. ``dev`` is force-pushed, so prepare.sh
re-fetches the base commit by sha when it is unreachable from the shared clone.

Registers ONLY its own number_interval key (and the dash-bundle spelling); it
never registers the bare ``Yeachan-Heo/oh-my-claudecode`` key, which the earlier
era files own via their router.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "oh_my_claudecode_1595_to_1430"
_BASE_TAG = "base-1595_to_1430"
_NODE_IMAGE = "node:20-bookworm"

_VITEST_REPORT = "/home/vitest-report.json"
_VITEST_FLAGS = "--no-file-parallelism --testTimeout=60000 --hookTimeout=60000"


_EMIT_TESTCASES_PY = r'''"""Turn a vitest JSON report into the TESTCASE lines parse_log consumes.

Only ``TESTCASE <STATUS> <identifier>`` lines are parsed by the harness; every
other line printed here is diagnostics. The identifier is
``<repo-relative file> > <describe...> > <test title>``, which is the shape
report.py's ``_test_name_matches_files`` expects for a JS/TS suite.
"""

import json
import os
import sys

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


# Runs once at PR-image build time (network available); leaves the tree detached
# at base.sha with node_modules populated so the graded stages never touch the
# network. Clean-tree asserts bracket the checkout only (npm ci may rewrite the
# lockfile; the prune runs afterwards).
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

if ! git cat-file -e __BASE_SHA__^{commit} 2>/dev/null; then
    git remote add origin "https://github.com/__ORG__/__REPO__.git" 2>/dev/null || true
    git fetch --quiet --no-tags origin __BASE_SHA__
fi

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

test -d node_modules
test -x node_modules/.bin/vitest
npx --no-install vitest --version
node -e "require('./package.json'); console.log('DEPS_OK')"
python3 -c "import json, sys; print('EMIT_TOOLCHAIN_OK')"
"""


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


# `|| STATUS=$?` on the runner only: a non-zero exit is the expected test-stage
# outcome. `test -s` is a hard gate that aborts if vitest produced no report.
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


# Shared base. Line 1 is the DockerfileEnhancer opt-out, so the infra the
# enhancer would inject (proxy ARGs, TLS/CA env, CA symlink farm, labels) is
# emitted here. BASE_COMMIT is declared but never referenced (referencing it
# would pin an image shared by every PR to one commit).
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


# Per-PR layer owns the pin + prune. The prune opens with a commit-scoped
# `git checkout --detach` (same-tree no-op) that preserves prepare.sh's work
# while detaching HEAD before every ref is deleted. No reset/clean/path-scoped
# checkout and no clean-tree assert from here on.
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


class OhMyClaudecodeEraImageBase1595To1430(Image):
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


class OhMyClaudecodeEraImageDefault1595To1430(Image):
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
        return OhMyClaudecodeEraImageBase1595To1430(self.pr, self._config)

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


@Instance.register("Yeachan-Heo", _INTERVAL_NAME)
class OH_MY_CLAUDECODE_1595_TO_1430(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeEraImageDefault1595To1430(self.pr, self._config)

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


# Dash-bundle spelling of this era's number_interval, so a JSONL row that
# carries it resolves directly. The bare `Yeachan-Heo/oh-my-claudecode` key is
# intentionally NOT registered here — the earlier era files own that router.
Instance.register("Yeachan-Heo", "1430-1595")(OH_MY_CLAUDECODE_1595_TO_1430)
