import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The repo's own CI pins the runtime exactly: both workflow files at the base
# commit (2021-03-04) run actions/setup-node with node-version: 14.15.1. Same
# pin here - not an era guess, the project's own number. The full Debian
# variant ships git, ca-certificates, yarn 1.x and a C toolchain, and the
# official image publishes linux/amd64 AND linux/arm64 (verified in the
# manifest).
#
# arm64 is additionally cheap for this repo: CI installs with
# --ignore-scripts, which skips every postinstall/node-gyp hook, and that is
# reproduced here - so nothing compiles natively on either architecture.
NODE_IMAGE = "node:14.15.1"

# Both patches live in packages/transactions (a yarn-workspaces + lerna
# monorepo), so grading is scoped to that package's own jest setup instead of
# lerna's full fan-out.
#
# The `cd packages/transactions &&` prefix is LOAD-BEARING, learned the hard
# way. A first version of this command ran jest from the repo root with
# --config/--rootDir flags, and every BigNum-using test failed with
# `TypeError: bn_js_1.default is not a constructor` - at every stage,
# baseline included. Cause: this ts-jest resolves the jest.config's
# `tsConfig: './tsconfig.json'` against the WORKING DIRECTORY, so from the
# repo root it silently compiled the tests with the wrong tsconfig (no
# esModuleInterop) and every `import BigNum from 'bn.js'` produced undefined.
# The repo's own CI runs jest through `lerna run test`, whose cwd is the
# package directory - reproduced here. Verified empirically: from inside the
# package the pristine base runs 103 passed / 0 failed.
#
#   * ts-jest compiles the package's TypeScript on the fly, and the tests
#     import '../src/...' directly - so the patched sources are exercised
#     without any per-stage build step. Sibling packages (@stacks/common,
#     @stacks/network) resolve to the dist/ that prepare.sh builds once; the
#     patches never touch them.
#   * --coverage=false mirrors the repo's own CI invocation
#     (`lerna run test ... -- -- --coverage=false`) and keeps the coverage
#     table out of the parsed log.
#   * `npx --no-install` refuses to download a modern jest if resolution ever
#     failed - absence must be a loud error, not a silent version drift.
TEST_CMD = (
    "cd packages/transactions && npx --no-install jest"
    " --ci --runInBand --verbose --coverage=false"
)

# Every byte this image emits at BUILD time is forced down to printable ASCII
# (plus tab/LF/CR). The harness streams `docker buildx` output through
# `subprocess` with `text=True` and no explicit encoding, so a Windows host
# decodes it with cp1252 - and any UTF-8 byte outside that map aborts the
# build. Runtime logs are decoded as UTF-8 explicitly, so only build-time
# commands are wrapped.
ASCII_FILTER = r"tr -cd '\11\12\15\40-\176'"

# Declared ONCE, in the base image; Docker propagates ENV to the PR image.
ENCODING_ENV = """ENV CI=true \\
    NO_COLOR=1 \\
    npm_config_color=false \\
    npm_config_progress=false"""


class ImageBase(Image):
    """Per-PR base: OS + Node toolchain + the repo frozen at BASE_COMMIT.

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

    No apt layer: the full Debian node image already ships git,
    ca-certificates and yarn 1.x (proven by the enhancer's own HTTPS clone
    succeeding), and this repo needs nothing else from the OS.
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
        return NODE_IMAGE

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

        return f"""FROM {image_name}

{self.global_env}

{ENCODING_ENV}

WORKDIR /home/

{code}

{self.clear_env}

"""


class ImageDefault(Image):
    """Per-PR layer: patches, stage scripts, and the bootstrapped monorepo."""

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

cd "/home/{repo}"
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

# Reproduce the repo's own CI bootstrap verbatim
# (.github/workflows/pull_request.yml at the base commit):
#   yarn --frozen-lockfile --ignore-scripts   <- --ignore-scripts skips every
#       postinstall/node-gyp hook, exactly as CI does; nothing native compiles
#       on either architecture
#   yarn lerna bootstrap                      <- links workspace packages
#   yarn build                                <- builds every package's dist/,
#       which is how sibling deps (@stacks/common, @stacks/network) resolve at
#       test time; the patches never touch those packages, so building once at
#       prepare time keeps the graded stages offline and byte-identical
# --network-timeout 600000: yarn's default 30s socket timeout is fragile
# under QEMU emulation (the arm64 build shares one emulated CPU with
# everything else) - a real ESOCKETTIMEDOUT on rxjs killed the first
# arm64 build of this image. Ten minutes per socket is generous, not
# masking: a genuinely dead network still fails, just not a slow one.
yarn --frozen-lockfile --ignore-scripts --network-timeout 600000
yarn lerna bootstrap
yarn build

# Warm ts-jest's transform cache for the graded package; the outcome is
# irrelevant to grading, hence `|| true`.
{test_cmd} > /dev/null 2>&1 || true
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

cd "/home/{repo}"
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

cd "/home/{repo}"
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

cd "/home/{repo}"
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


@Instance.register("stx-labs", "stacks.js")
class StacksJs(Instance):
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

        # jest --verbose: one leaf line per test under a per-file PASS/FAIL
        # banner, keyed as <file>::<name>. A leaf MUST carry jest's own result
        # glyph - runtime logs are decoded as UTF-8 (only BUILD output goes
        # through the ASCII filter), and requiring the glyph is what keeps
        # jest's suite-level error prose ("Cannot find module ...", "Your test
        # suite must contain at least one test.") out of the classified sets.
        # This is the parser proven on the codeBelt/nextjs-mobx instance,
        # including its noise filter for failure-detail lines.
        re_file = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.[jt]sx?)\b")
        re_leaf = re.compile(r"^\s{2,}(✓|✕|✗|×)\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_todo = re.compile(r"^\s{2,}(?:○\s*)?(?:todo|skipped)[:\s]\s*(.+?)\s*$", re.I)
        re_noise = re.compile(
            r"""^(?:
                  \d+\s*\|
                | at\s
                | [-+]\s*\d*\s*\|
                | [\[\]{}"']
                | (?:Expected|Received|Difference|Object|Array|Error|TypeError)\b
                | [-=]{3,}
                )""",
            re.X,
        )
        re_code_like = re.compile(
            r"(?:expect\(|assert\(|\.[jt]s:\d+:\d+|=>\s|\}\s*\)|\bfunction\b)"
        )

        def is_noise(name: str) -> bool:
            return bool(re_noise.match(name)) or bool(re_code_like.search(name))

        failing_names = set(
            m.group(1).strip()
            for m in re.finditer(r"^\s*(?:✕|x|\*)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", log, re.M)
            if not is_noise(m.group(1).strip())
        )

        current_file = ""

        def record(name: str, status: str) -> None:
            if status == "pass":
                if name in failed_tests:
                    return
                skipped_tests.discard(name)
                passed_tests.add(name)
            elif status == "fail":
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            else:
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        for raw_line in log.splitlines():
            line = raw_line.rstrip()

            m = re_file.match(line)
            if m:
                current_file = m.group(2)
                continue

            if not current_file or not line.strip():
                continue
            if line.lstrip().startswith(
                ("PASS", "FAIL", "Tests:", "Test Suites:", "Snapshots:", "Time:", "Ran all")
            ):
                continue

            m = re_todo.match(line)
            if m:
                record(f"{current_file}::{m.group(1).strip()}", "skip")
                continue

            m = re_leaf.match(line)
            if m:
                glyph, name = m.group(1), m.group(2).strip()
                if not name or is_noise(name):
                    continue
                test_id = f"{current_file}::{name}"
                is_fail = glyph in ("✕", "✗", "×") or name in failing_names
                record(test_id, "fail" if is_fail else "pass")

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
