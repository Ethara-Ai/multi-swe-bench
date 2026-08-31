import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The base commit is 2025-07-10 and the workspace pins rust-version = 1.81.0
# (MSRV). rust:1.89-trixie is the era-current stable on the era-current Debian
# - and the Debian RELEASE is the load-bearing choice here, not the Rust
# version: neqo-crypto refuses NSS older than MINIMUM_NSS_VERSION = 3.110
# (neqo-crypto/min_version.txt), bookworm ships NSS 3.87 (too old -> build.rs
# would try to compile all of NSS from source, a huge C build that would be
# agony under arm64 emulation), while trixie ships exactly 2:3.110 - verified
# against the live apt archive before this config was written. With system NSS
# satisfying the minimum, the repo's own CI path (`apt-get install
# libnss3-dev pkg-config`) is reproduced verbatim and nothing native is built
# beyond the crates themselves. The image publishes linux/amd64 AND
# linux/arm64 (verified in the manifest).
RUST_IMAGE = "rust:1.89-trixie"

# Whole workspace minus the fuzz crate: fuzz targets want cargo-fuzz/nightly
# machinery that the repo's own CI also keeps out of the plain test job.
# --no-fail-fast so one failing crate cannot hide results from the others -
# parse_log needs every crate's outcome at every stage.
TEST_CMD = "cargo test --workspace --exclude fuzz --no-fail-fast"

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR): the harness streams `docker buildx` output through
# `subprocess` with `text=True` and no explicit encoding, so a Windows host
# decodes it with cp1252 and any UTF-8 byte outside that map aborts the build
# (cargo's progress output is full of them). Runtime logs are decoded as UTF-8
# explicitly, so only build-time commands are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# Declared ONCE, in the base image; Docker propagates ENV to the PR image.
ENCODING_ENV = """ENV CARGO_TERM_COLOR=never \\
    CARGO_TERM_PROGRESS_WHEN=never \\
    RUST_BACKTRACE=1 \\
    NO_COLOR=1"""


class ImageBase(Image):
    """Per-PR base: OS + Rust toolchain + system NSS + the repo at BASE_COMMIT.

    Tagged `base-pr-<N>`, so the tag names the pull request whose code is
    inside it (QC item P1). A single shared `base` tag cannot make that
    promise - the first PR to build it freezes it, and every later PR silently
    inherits the wrong commit while the tag still reads `base`.

    The clone below is deliberately the bare `RUN git clone <url> /home/<repo>`
    form: that exact shape is what DockerfileEnhancer._standardize_repo_fetch
    matches, and its rewrite supplies the REPO_URL/BASE_COMMIT clone, the
    checkout, the history-sanitising scrub with its four integrity asserts,
    and the final CMD. Decorate that line and the enhancer stops recognising
    it, so the hardening block is silently never injected.
    """

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
        return RUST_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # The apt line mirrors the repo's own CI: libnss3-dev + pkg-config is
        # how upstream gets its crypto (build.rs takes the system-NSS path when
        # `pkg-config --modversion nss` >= 3.110, which trixie's 3.110 is).
        # clang/libclang-dev serve neqo-crypto's bindgen, which needs libclang
        # at build time - present on GitHub's runners implicitly, explicit
        # here. The rust image ships git and a C toolchain already.
        return f"""FROM {image_name}

{self.global_env}

{ENCODING_ENV}

WORKDIR /home/

RUN /bin/bash -o pipefail -c "( apt-get update && apt-get install -y --no-install-recommends git ca-certificates pkg-config libnss3-dev clang libclang-dev && rm -rf /var/lib/apt/lists/* ) 2>&1 | {ASCII_FILTER}"

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """Per-PR layer: the patches, the stage scripts, and the warmed target/."""

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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -e

# Integrity guard. prepare.sh calls this immediately after `git reset --hard`
# and again after `git checkout <BASE_COMMIT>`, so a tree that did not actually
# come back clean aborts the BUILD instead of being baked into the image and
# silently contaminating all three graded stages.
#
# `git status --porcelain` is empty only when there is nothing modified, staged
# or untracked - deliberately stricter than `git diff --quiet`, because the
# failure this catches is usually a leftover UNTRACKED file (`git clean -qfd`
# does not remove ignored files without -x).
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
# Assert the reset really produced a clean tree, so a leftover file cannot be
# baked into the image and silently inherited by all three graded stages.
bash /home/check_git_changes.sh

# The base image is frozen at ONE commit and has had its origin remote stripped
# by the hardening block, so a commit that is not already present cannot be
# resolved locally and `git fetch origin` has no remote to use. Ask GitHub for
# that exact commit by sha over the full URL. A fetch drags fresh git objects
# into an image whose history the base deliberately stripped, so the block
# below re-runs the scrub in exactly that case. On this instance the base was
# built from this very sha, so the fetch never runs.
FETCHED=0
if ! git cat-file -e {sha} 2>/dev/null; then
    git fetch --quiet https://github.com/{org}/{repo}.git {sha}
    FETCHED=1
fi
git checkout {sha}
bash /home/check_git_changes.sh

if [ "$FETCHED" = "1" ]; then
    git checkout --detach {sha}
    git remote remove origin 2>/dev/null || true
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d
    git reflog expire --expire=now --all
    git reflog expire --expire-unreachable=now --all
    git gc --prune=now --aggressive
    git repack -a -d -l --quiet
    rm -f .git/objects/info/alternates
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
    test -z "$(git remote)"
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
fi

# Warm run: downloads the whole crates.io dependency tree into ~/.cargo and
# fills target/ with compiled deps (both live outside the tracked tree -
# ~/.cargo entirely, target/ gitignored - so the guard stays honest and the
# graded stages only recompile what the patches change). The outcome is
# irrelevant to grading, hence `|| true`.
{test_cmd} || true
git reset --hard
git clean -qfd
bash /home/check_git_changes.sh
""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    org=self.pr.org,
                    test_cmd=TEST_CMD,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uxo pipefail

cd /home/{repo}
git reset --hard
git clean -qfd
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

# `-o pipefail` so a failing prepare.sh still fails the build: without it the
# pipeline would report the exit status of `tr`, which always succeeds, and a
# broken image would be published as if it were good.
RUN /bin/bash -o pipefail -c "bash /home/prepare.sh 2>&1 | {ASCII_FILTER}"

{self.clear_env}

"""


@Instance.register("mozilla", "neqo")
class Neqo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        log = ansi_escape.sub("", test_log)

        # cargo test prints one section per test binary:
        #     Running tests/server.rs (target/debug/deps/server-ab12cd)
        #     Running unittests src/lib.rs (target/debug/deps/neqo_transport-...)
        #     Doc-tests neqo_common
        # then one line per test:
        #     test resumption_token ... ok
        #     test send_receive ... FAILED
        #     test slow_one ... ignored
        # Bare test names collide across a 9-crate workspace, so every id is
        # prefixed with its binary's label. The label is taken from the deps/
        # binary name with its trailing -<hash> stripped (stable across
        # rebuilds), falling back to the source path.
        re_running = re.compile(
            r"^\s*Running\s+(?:unittests\s+)?(\S+)\s+\(target/[^/]+/deps/([A-Za-z0-9_.-]+?)(?:-[0-9a-f]{7,})?\)"
        )
        re_doctest = re.compile(r"^\s*Doc-tests\s+(\S+)")
        # Lazy `.+?` so doc-test names survive intact - they look like
        # `test src/lib.rs - qlog (line 10) ... ok`, which a plain \S+ name
        # match would silently drop. Test names never contain " ... ", so the
        # lazy match always stops at the status separator.
        re_result = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)")

        current = ""
        for raw in log.splitlines():
            line = raw.rstrip()

            m = re_running.match(line)
            if m:
                current = m.group(2) or m.group(1)
                continue
            m = re_doctest.match(line)
            if m:
                current = f"doc::{m.group(1)}"
                continue

            m = re_result.match(line.strip())
            if not m:
                continue
            name, status = m.group(1), m.group(2)
            test_id = f"{current}::{name}" if current else name
            if status == "ok":
                if test_id not in failed_tests:
                    skipped_tests.discard(test_id)
                    passed_tests.add(test_id)
            elif status == "FAILED":
                passed_tests.discard(test_id)
                skipped_tests.discard(test_id)
                failed_tests.add(test_id)
            else:  # ignored
                if test_id not in passed_tests and test_id not in failed_tests:
                    skipped_tests.add(test_id)

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
