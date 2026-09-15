import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_RUST_IMAGE = "rust:1.80-bookworm"

_BASE_TAG = "base-3439_to_5461"

_ENV = """\
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=10
"""

_CRATES_FN = """\
crates_for() {
  { grep -E '^diff --git a/' "$1" || true; } | sed -E 's|^diff --git a/([^ ]+) b/.*$|\\1|' | while read -r f; do
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ ! -f "$d/Cargo.toml" ]; do
      d=$(dirname "$d")
    done
    if [ "$d" != "." ] && grep -q '^\\[package\\]' "$d/Cargo.toml"; then
      sed -n 's/^name *= *"\\([^"]*\\)".*/\\1/p' "$d/Cargo.toml" | head -n 1
    fi
  done | sort -u
}
"""

_CHECK_GIT_CHANGES = """\
#!/bin/bash
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

_PREPARE = """\
#!/bin/bash
set -euo pipefail

__ENV__
cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

__CRATES_FN__
TOOLCHAIN=$(sed -n 's/^channel *= *"\\([^"]*\\)".*/\\1/p' rust-toolchain.toml)
test -n "$TOOLCHAIN"
rustup toolchain install "$TOOLCHAIN" --profile minimal
rustc --version
cargo --version

cargo fetch --locked

GATE_CRATES=$(crates_for /home/test.patch)
GATE_MODE=test
if [ -z "$GATE_CRATES" ]; then
  GATE_CRATES=$(crates_for /home/fix.patch)
  GATE_MODE=fallback
fi
test -n "$GATE_CRATES"
for CRATE in $GATE_CRATES; do
  if [ "$GATE_MODE" = "test" ]; then
    cargo test -p "$CRATE" --all-features --tests --no-run --locked
  else
    cargo test -p "$CRATE" --tests --no-run --locked
  fi
done
echo "DEPS_OK $TOOLCHAIN $GATE_MODE" $GATE_CRATES
"""

_STAGE = """\
#!/bin/bash
set -eo pipefail

__ENV__
cd /home/__REPO__
__APPLY__
__CRATES_FN__
STATUS=0
TEST_CRATES=$(crates_for /home/test.patch)
if [ -z "$TEST_CRATES" ]; then
  echo "=== NO TEST CRATE PRESENT IN TREE"
fi
for CRATE in $TEST_CRATES; do
  echo "=== CRATE $CRATE"
  cargo test -p "$CRATE" --all-features --tests --no-fail-fast || STATUS=$?
done
exit $STATUS
"""

_PRUNE = """\
RUN set -eux; \\
    cd /home/__REPO__; \\
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    rm -f .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/__REPO__/.gitmodules ]; then \\
        cd /home/__REPO__ && git submodule foreach --recursive ' \\
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


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("__APPLY__", apply)
        .replace("__ENV__", _ENV)
        .replace("__CRATES_FN__", _CRATES_FN)
        .replace("__BASE_SHA__", pr.base.sha)
        .replace("__REPO__", pr.repo)
    )


class Icu4x3439To5461ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = DockerfileEnhancer._infrastructure_block(self, _RUST_IMAGE)
        repo = self.pr.repo
        return (
            f"{DockerfileEnhancer.SYNTAX_DIRECTIVE}\n\n"
            f"FROM {_RUST_IMAGE}\n\n"
            f"{infra}\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates build-essential pkg-config \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "RUN git config --global --add safe.directory '*'\n\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
            f"    cd /home/{repo} && git rev-parse HEAD >/dev/null\n\n"
            'CMD ["/bin/bash"]\n'
        )


class Icu4x3439To5461ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return Icu4x3439To5461ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_STAGE, self.pr)),
            File(
                ".",
                "test-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch\n",
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _STAGE,
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n\n"
            "RUN bash /home/prepare.sh\n\n"
            f"{_render(_PRUNE, self.pr)}"
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_CRATE_RE = re.compile(r"^=== CRATE (\S+)$")

_RUNNING_RE = re.compile(r"^Running (?:unittests )?(\S+) \(")

_TEST_RE = re.compile(r"^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED|ignored)(?:,.*)?$")


@Instance.register("unicode-org", "icu4x_3439_to_5461")
class ICU4X_3439_TO_5461(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Icu4x3439To5461ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        crate = ""
        target = ""
        for raw_line in _ANSI_RE.sub("", test_log).split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            crate_match = _CRATE_RE.match(line)
            if crate_match:
                crate = crate_match.group(1)
                target = ""
                continue
            running_match = _RUNNING_RE.match(line)
            if running_match:
                target = running_match.group(1)
                continue
            test_match = _TEST_RE.match(line)
            if not test_match:
                continue
            name = f"{crate}::{target}::{test_match.group(1)}"
            status = test_match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "ok":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

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
