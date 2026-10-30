from __future__ import annotations

import json
import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# immerjs/immer, main. Single era (first dataset: PR 1328, Oct 2026).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# every PR of the repo. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base"

# package.json declares no engines and there is no .nvmrc; vitest ^3.2 needs
# node >= 18, and 22 is the current LTS.
NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/immer"

# Yarn classic: yarn.lock with no .yarnrc.yml and no packageManager field. The node
# image ships yarn 1.22, so nothing else is installed.
#
# Tests are vitest (`vitest run`, the repo's test:src script), run from source. The
# repo's full `yarn test` also builds with tsup and runs flow; neither grades a
# behaviour change, so only the vitest suites the test patch touches are run.
# Results are read from vitest's own --reporter=json report rather than its console
# output, so every test is named by its file and full describe chain.
VITEST_FLAGS = "--reporter=json"

# vitest.config.ts: include ["**/__tests__/**/*.[jt]s?(x)"].
TEST_FILE_RE = re.compile(r"(^|/)__tests__/(?:.*/)?[^/]+\.[jt]sx?$")

# Marker line placed directly before the vitest JSON report in every stage log, so
# parse_log reads exactly that report and nothing else in the log.
REPORT_MARKER = "@@IMMER_VITEST_JSON@@"

# Suite-level key: emitted from vitest's per-file verdict. A test file whose module
# fails to load (e.g. it imports something only the fix adds) reports no assertions
# at all; this key still records that the file ran and how it ended.
SUITE_TEST_NAME = "<suite>"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)


def _test_patch_paths(pr: PullRequest) -> list[str]:
    """Post-patch paths the test patch touches; deleted files are dropped."""
    paths = set()
    for block in re.split(r"(?m)^(?=diff --git )", pr.test_patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header and "\ndeleted file mode" not in block:
            paths.add(header.group(2))
    return sorted(paths)


def test_files(pr: PullRequest) -> list[str]:
    """The vitest files this PR grades.

    A test file the patch touches is run as is; a snapshot
    `<dir>/__snapshots__/<name>.snap` runs the test file `<dir>/<name>`. Anything
    else (helpers, fixtures, docs) selects nothing.
    """
    files = set()
    for path in _test_patch_paths(pr):
        directory, name = posixpath.split(path)
        if posixpath.basename(directory) == "__snapshots__" and name.endswith(".snap"):
            files.add(posixpath.join(posixpath.dirname(directory), name[: -len(".snap")]))
        elif TEST_FILE_RE.search(path):
            files.add(path)
    return sorted(files)


class ImmerImageBase(Image):
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
    YARN_CACHE_FOLDER=/root/.cache/yarn
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
# Exactly the lockfile's versions. Not tolerated: a failed install is a failed build.
yarn --version
yarn install --frozen-lockfile --non-interactive

# ========== Section 3: GATE (non-tolerant, last) ==========
node --version
yarn --silent vitest --version
# The runner loads vitest.config.ts and collects the suite from source.
yarn --silent vitest list --filesOnly > /tmp/vitest_files.log
test -s /tmp/vitest_files.log
rm -f /tmp/vitest_files.log
echo DEPS_OK
"""


# The one test command every stage runs. A test file the test patch creates exists
# only once that patch is applied, so the baseline simply runs fewer files instead
# of failing on a missing path.
TEST_CMD = r"""# A patch that changes the dependency manifests leaves node_modules from the base
# commit stale. Reinstall exactly when the applied patches touched them; the
# baseline and any patch that leaves them alone skip this. Not tolerated: a failed
# install fails the stage.
if ! git diff --quiet HEAD -- package.json yarn.lock; then
    yarn install --non-interactive
fi

_files=()
for _file in [[TEST_FILES]]; do
    if [ -f "${_file}" ]; then
        _files+=("${_file}")
    fi
done

echo "[[REPORT_MARKER]]"
if [ "${#_files[@]}" -eq 0 ]; then
    echo '{"testResults":[]}'
    exit 0
fi
# The JSON report is vitest's whole stdout, on one line; its progress and errors
# go to stderr. vitest exits non-zero when any test fails, and that status is the
# stage's.
yarn --silent vitest run [[VITEST_FLAGS]] "${_files[@]}"
"""


class ImmerImageDefault(Image):
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
        return ImmerImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_FILES]]", " ".join(f"'{f}'" for f in test_files(self.pr)))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[VITEST_FLAGS]]", VITEST_FLAGS)
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
        # the base must keep full history so every PR can reach its own commit.
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


@Instance.register("immerjs", "immer")
class Immer(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImmerImageDefault(self.pr, self._config)

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

        # The report is the first line after the marker that parses as a vitest
        # result object. vitest's stderr is interleaved with it and can itself hold
        # lines starting with `{`, so a line is only accepted once it parses and
        # carries `testResults`. A truncated or missing report yields no tests for
        # this stage rather than names guessed from console text.
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
