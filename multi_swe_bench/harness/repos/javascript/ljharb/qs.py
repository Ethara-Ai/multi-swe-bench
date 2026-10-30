from __future__ import annotations

import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ljharb/qs, main. Single era (first dataset: PR 623, Oct 2026).
#
# SHARED BASE architecture: image_tag() is PR-independent, so one base image serves
# every PR of the repo. The base therefore keeps FULL history and never references
# BASE_COMMIT; the pin lives in prepare.sh and the history scrub in the PR layer.
# The `# syntax` directive on line 1 of the base Dockerfile is what keeps
# DockerfileEnhancer from rewriting the clone into a pinned, scrubbed single
# commit -- which would make every PR but one unbuildable.
BASE_TAG = "base"

# engines.node is ">=0.6", but the dev toolchain (eslint 10, tape 5) needs a
# current node; 22 is the current LTS.
NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/qs"

# npm with no lockfile: the repo's .npmrc sets package-lock=false, so `npm install`
# resolves the package.json ranges at install time.
#
# Tests are tape (`tests-only`: `nyc tape 'test/**/*.js'`), run directly without
# nyc. `npm test` also runs eslint, evalmd and `npm audit` (network); none of them
# grades a behaviour change. Each graded file runs in its own tape process, framed
# by markers, so every result is attributed to its file.
FILE_MARKER = "@@QS_TAPE_FILE@@"
EXIT_MARKER = "@@QS_TAPE_EXIT@@"

# File-level key: emitted from tape's exit status for the file. A file whose module
# fails to load (e.g. it requires something only the fix adds) prints no test
# blocks at all; this key still records that the file ran and how it ended.
FILE_TEST_NAME = "<file>"

TEST_FILE_RE = re.compile(r"^test/.+\.js$")

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
    """The tape files this PR grades: every test/**/*.js the test patch touches."""
    return sorted(
        path for path in _test_patch_paths(pr) if TEST_FILE_RE.match(posixpath.normpath(path))
    )


class QsImageBase(Image):
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
    npm_config_audit=false \\
    npm_config_fund=false \\
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
# No lockfile by design (.npmrc: package-lock=false). Not tolerated: a failed
# install is a failed build.
npm --version
npm install

# ========== Section 3: GATE (non-tolerant, last) ==========
node --version
test -x node_modules/.bin/tape
# The runner and the package itself both load.
node -e "require('tape'); var qs = require('./'); if (typeof qs.stringify !== 'function' || typeof qs.parse !== 'function') { throw new Error('qs did not load'); }"
echo DEPS_OK
"""


# The one test command every stage runs. Each graded file runs in its own tape
# process between markers; a file that does not exist in this stage is skipped.
TEST_CMD = r"""# A patch that changes the dependency manifest leaves node_modules from the base
# commit stale. Reinstall exactly when the applied patches touched it; the
# baseline and any patch that leaves it alone skip this. Not tolerated: a failed
# install fails the stage.
if ! git diff --quiet HEAD -- package.json; then
    npm install
fi

# Every file runs even when an earlier one fails; `|| _rc=$?` records each status
# (printed for parse_log) instead of swallowing it, and the stage exits non-zero
# if any file failed.
_status=0
for _file in [[TEST_FILES]]; do
    [ -f "${_file}" ] || continue
    echo "[[FILE_MARKER]] ${_file}"
    _rc=0
    node_modules/.bin/tape "${_file}" 2>&1 || _rc=$?
    echo "[[EXIT_MARKER]] ${_file} ${_rc}"
    [ "${_rc}" -eq 0 ] || _status=1
done
exit ${_status}
"""


class QsImageDefault(Image):
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
        return QsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[TEST_FILES]]", " ".join(f"'{f}'" for f in test_files(self.pr)))
            .replace("[[FILE_MARKER]]", FILE_MARKER)
            .replace("[[EXIT_MARKER]]", EXIT_MARKER)
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


@Instance.register("ljharb", "qs")
class Qs(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return QsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        file_re = re.compile(r"^" + re.escape(FILE_MARKER) + r" (\S+)$")
        exit_re = re.compile(r"^" + re.escape(EXIT_MARKER) + r" (\S+) (\d+)$")
        # tape prints every test, nested or not, as a flat `# <name>` header.
        header_re = re.compile(r"^# (.+?)\s*$")
        # tape's closing summary lines are headers too; they are not tests.
        summary_re = re.compile(r"^# (?:tests|pass|fail|ok|todo|skip)\b")
        assert_re = re.compile(r"^(ok|not ok) \d+\b(.*)$")
        skip_re = re.compile(r"#\s*(?:SKIP|TODO)\b", re.I)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        current_file: Optional[str] = None
        seen: dict[str, int] = {}
        # Per tape test (one `# <name>` block): [any non-skipped ok, any failure, any skip]
        blocks: dict[str, list[bool]] = {}
        current_block: Optional[str] = None

        def close_file() -> None:
            for name, (ok, bad, skip) in blocks.items():
                if bad:
                    failed.add(name)
                elif ok:
                    passed.add(name)
                elif skip:
                    skipped.add(name)
            blocks.clear()
            seen.clear()

        for raw in log.splitlines():
            line = raw.rstrip()

            file_match = file_re.match(line)
            if file_match:
                close_file()
                current_file = file_match.group(1)
                current_block = None
                continue

            exit_match = exit_re.match(line)
            if exit_match:
                name = exit_match.group(1)
                close_file()
                # The file-level verdict is tape's own exit status for the file.
                if exit_match.group(2) == "0":
                    passed.add(f"{name}::{FILE_TEST_NAME}")
                else:
                    failed.add(f"{name}::{FILE_TEST_NAME}")
                current_file = None
                current_block = None
                continue

            if current_file is None:
                continue

            header_match = header_re.match(line)
            if header_match and not summary_re.match(line):
                title = header_match.group(1)
                # tape names need not be unique within a file; repeats are
                # numbered in run order, so `name`, `name #2`, `name #3` ...
                seen[title] = seen.get(title, 0) + 1
                suffix = "" if seen[title] == 1 else f" #{seen[title]}"
                current_block = f"{current_file}::{title}{suffix}"
                blocks.setdefault(current_block, [False, False, False])
                continue

            assert_match = assert_re.match(line)
            if assert_match and current_block is not None:
                state = blocks[current_block]
                if skip_re.search(assert_match.group(2)):
                    state[2] = True
                elif assert_match.group(1) == "ok":
                    state[0] = True
                else:
                    state[1] = True

        # A file whose exit marker never printed (output cut off) is closed with
        # what it reported, and gets no file-level key.
        close_file()

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
