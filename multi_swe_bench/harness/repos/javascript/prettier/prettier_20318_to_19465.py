from __future__ import annotations

import json
import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# INTERVAL: prettier/prettier PRs 19465..20318 (Jun-Oct 2026, main).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# the whole shard. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base-20318_to_19465"

# package.json declares engines.node ">=22" at all 28 base commits; prettier.py's
# node:20 cannot run this era.
NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/prettier"

# Yarn berry at every base commit: packageManager yarn@4.17.0..4.18.1, the matching
# release vendored under .yarn/releases (yarnPath), nodeLinker: node-modules and
# enableScripts: false. Corepack selects the pinned release per commit.
#
# Tests are plain jest, run from source (no build step). Results are read from
# jest's own --json report rather than its console output: prettier's format
# tests are hundreds of near-identical titles ("[babel] format") per file, so only
# the structured report carries enough context to name every test uniquely.
JEST_FLAGS = "--ci --json --runInBand --passWithNoTests"

# Marker line placed directly before the jest JSON report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@PRETTIER_JEST_JSON@@"

# Suite-level key: emitted from jest's per-file verdict. A test file whose module
# fails to load (e.g. it imports something only the fix adds) reports no
# assertions at all; this key still records that the file ran and how it ended.
SUITE_TEST_NAME = "<suite>"

FORMAT_ROOT = "tests/format/"
FORMAT_TEST = "format.test.js"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _test_patch_paths(pr: PullRequest) -> list[str]:
    """Post-patch paths the test patch touches; deleted files are dropped."""
    paths = set()
    for block in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header and "\ndeleted file mode" not in block:
            paths.add(header.group(2))
    return sorted(paths)


def added_test_files(pr: PullRequest) -> list[str]:
    """Paths the test patch creates (`new file mode`)."""
    added = set()
    for block in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header and "\nnew file mode" in block:
            added.add(header.group(2))
    return sorted(added)


def test_selectors(pr: PullRequest) -> list[str]:
    """One entry per graded jest file, as `|`-separated candidate paths.

    The test script picks the FIRST candidate that exists in the tree at run time,
    so a stage where the file is not there yet simply runs fewer files:
      - a test file itself: just that path;
      - `<dir>/__snapshots__/<name>.snap`: the test file `<dir>/<name>`;
      - a format fixture: the nearest `format.test.js` at or above its directory
        (every fixture directory is run by its own format.test.js).
    Paths outside tests/ (scripts, CI workflows) select nothing.
    """
    selectors = set()
    for path in _test_patch_paths(pr):
        if not path.startswith("tests/"):
            continue
        directory, name = posixpath.split(path)
        if posixpath.basename(directory) == "__snapshots__" and name.endswith(".snap"):
            selectors.add(posixpath.join(posixpath.dirname(directory), name[: -len(".snap")]))
        elif name.endswith(".js") and (
            name.endswith(".test.js") or "/__tests__/" in path or path.startswith("tests/unit/")
        ):
            selectors.add(path)
        elif path.startswith(FORMAT_ROOT):
            chain = []
            while directory.startswith(FORMAT_ROOT.rstrip("/")) and directory != FORMAT_ROOT.rstrip("/"):
                chain.append(posixpath.join(directory, FORMAT_TEST))
                directory = posixpath.dirname(directory)
            if chain:
                selectors.add("|".join(chain))
    return sorted(selectors)


class PrettierImageBase(Image):
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
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\
    YARN_NODE_LINKER=node-modules \\
    YARN_ENABLE_TELEMETRY=0
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
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*
RUN git config --global --add safe.directory '*'
RUN corepack enable
WORKDIR /home/
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
# Fetch the yarn release this commit pins (packageManager) into corepack's cache
# now, so no stage needs the network to start yarn.
corepack install
yarn --version

# Not --immutable: YARN_ENABLE_IMMUTABLE_INSTALLS=false lets yarn reconcile the
# lockfile if it must. Not tolerated: a failed install is a failed build.
yarn install

# ========== Section 3: GATE (non-tolerant, last) ==========
node --version
yarn jest --version
# The test runner resolves its ESM config, and prettier loads from source.
node --input-type=module -e "await import('[[REPO_DIR]]/jest.config.js'); const p = await import('[[REPO_DIR]]/index.js'); if (typeof p.format !== 'function') { throw new Error('prettier did not load'); }"
echo DEPS_OK
"""


# The one test command every stage runs. Each selector is a `|`-separated list of
# candidate test files, nearest first. The chosen file is the first candidate that
# exists in the tree OR that the test patch creates -- i.e. the file it resolves to
# once the test patch is applied -- so every stage resolves a selector to the SAME
# file. That file runs only in stages where it exists: a test file the test patch
# creates is absent from the baseline, which then runs nothing for that selector
# rather than falling back to an unrelated parent test file.
TEST_CMD = r"""# A patch that changes the dependency manifests (several fixes bump a parser such
# as angular-html-parser, or add one such as mdast-util-directive) leaves
# node_modules from the base commit stale, and the fixed code then fails to import.
# Reinstall exactly when the applied patches touched them; the baseline and any
# patch that leaves them alone skip this. Not tolerated: a failed install fails
# the stage.
if ! git diff --quiet HEAD -- package.json yarn.lock; then
    yarn install
fi

_added=" [[ADDED]] "
_files=()
for _selector in [[SELECTORS]]; do
    IFS='|' read -r -a _candidates <<< "${_selector}"
    for _candidate in "${_candidates[@]}"; do
        if [ -f "${_candidate}" ] || [[ "${_added}" == *" ${_candidate} "* ]]; then
            if [ -f "${_candidate}" ]; then
                # Two selectors can resolve to one file (a fixture and the test beside it).
                case " ${_files[*]} " in
                    *" ${_candidate} "*) ;;
                    *) _files+=("${_candidate}") ;;
                esac
            fi
            break
        fi
    done
done

echo "[[REPORT_MARKER]]"
if [ "${#_files[@]}" -eq 0 ]; then
    echo '{"testResults":[]}'
    exit 0
fi
# The JSON report goes to stdout on its own line; jest's progress output goes to
# stderr. Jest exits non-zero when any test fails, and that status is the stage's.
yarn jest [[JEST_FLAGS]] --runTestsByPath "${_files[@]}"
"""


class PrettierImageDefault(Image):
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
        return PrettierImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[SELECTORS]]", " ".join(f"'{s}'" for s in test_selectors(self.pr)))
            .replace("[[ADDED]]", " ".join(added_test_files(self.pr)))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[JEST_FLAGS]]", JEST_FLAGS)
        )

    def files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
            "fix-run.sh": (
                "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            ),
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


PR_LO, PR_HI = 19465, 20318


@Instance.register("prettier", "prettier_20318_to_19465")
class PRETTIER_20318_TO_19465(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PrettierImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        buckets: dict[str, set[str]] = {
            "PASSED": set(),
            "FAILED": set(),
            "SKIPPED": set(),
        }
        outcome = {
            "passed": "PASSED",
            "failed": "FAILED",
            "pending": "SKIPPED",
            "skipped": "SKIPPED",
            "todo": "SKIPPED",
            "disabled": "SKIPPED",
        }

        # The report is the first line after the marker that parses as a jest
        # result object. jest's progress output (stderr) is interleaved with it, and
        # a snapshot diff can itself contain lines starting with `{` (`{{ value }}`
        # in an Angular/Vue template), so a line is only accepted once it parses
        # and carries `testResults`. A truncated or missing report yields no tests
        # for this stage rather than names guessed from console text.
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
                if isinstance(parsed, dict) and "testResults" in parsed:
                    report = parsed
                    break
            break

        for suite in (report or {}).get("testResults") or []:
            file_path = str(suite.get("name") or "")
            if file_path.startswith(REPO_DIR + "/"):
                file_path = file_path[len(REPO_DIR) + 1 :]
            if not file_path:
                continue

            # Every key is `<file>::<describe> > ... > <title>`: the file plus the
            # whole describe chain, never timing or counts, so the same test has
            # the same name in every stage.
            for assertion in suite.get("assertionResults") or []:
                titles = list(assertion.get("ancestorTitles") or []) + [
                    str(assertion.get("title") or "")
                ]
                bucket = outcome.get(str(assertion.get("status") or ""))
                if bucket:
                    buckets[bucket].add(f"{file_path}::" + " > ".join(titles))

            suite_status = str(suite.get("status") or "")
            if suite_status == "passed":
                buckets["PASSED"].add(f"{file_path}::{SUITE_TEST_NAME}")
            elif suite_status == "failed":
                buckets["FAILED"].add(f"{file_path}::{SUITE_TEST_NAME}")

        passed = buckets["PASSED"]
        failed = buckets["FAILED"]
        skipped = buckets["SKIPPED"]

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


# ---------------------------------------------------------------------------
# Routing, owned entirely by this file -- prettier.py is not modified.
# ---------------------------------------------------------------------------
# Instance.create() keys on f"{org}/{number_interval}" and falls back to
# f"{org}/{repo}" only when number_interval is empty. These 28 records carry no
# number_interval, so they arrive on the bare name "prettier/prettier", which
# prettier.py registers to its own legacy class.
#
# Instance.register() overwrites rather than erroring, so this module must be
# imported AFTER prettier.py. The incumbent class is captured first and every PR
# outside this interval is handed straight back to it, so no other prettier record
# changes behaviour. Records that DO carry a number_interval never reach here at
# all -- they key directly on their own interval.
_INCUMBENT = Instance._registry.get("prettier/prettier")


@Instance.register("prettier", "prettier")
class PrettierBareName(Instance):
    """Not an implementation -- a router for the bare "prettier/prettier" key.

    __new__ returns an instance of the owning class instead of self, so the
    harness never sees this type.
    """

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if PR_LO <= pr.number <= PR_HI:
            return PRETTIER_20318_TO_19465(pr, config, *args, **kwargs)

        # Outside this era: hand back to whoever held the bare name before us.
        if _INCUMBENT is None:
            raise ValueError(
                f"prettier/prettier#{pr.number} falls outside the "
                f"{PR_LO}..{PR_HI} interval this adapter serves, and no previous "
                f"holder of the bare 'prettier/prettier' key was captured at "
                f"import time. Import "
                f"multi_swe_bench.harness.repos.javascript.prettier.prettier "
                f"before this module (see prettier/__init__.py)."
            )
        return _INCUMBENT(pr, config, *args, **kwargs)
