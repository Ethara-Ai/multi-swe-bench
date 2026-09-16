import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The graded tests for this era live in the Electron desktop app (Vitest + jsdom),
# not in the Rust crates, so every script works from this sub-directory.
_UI_DIR = "ui/desktop"

# Identical in run.sh / test-run.sh / fix-run.sh. The verbose reporter prints one
# line per test: "✓|×|↓ <file> > <describe...> > <test> [<n>ms]".
# testTimeout/hookTimeout are raised from vitest's 5s default and failures are
# retried: several jsdom suites (e.g. ExtensionModal) occasionally blow the 5s
# budget on a loaded machine, which would otherwise show up as a PASS -> FAIL
# transition between stages and invalidate an otherwise good instance.
_TEST_CMD = (
    "npx vitest run --reporter=verbose"
    " --testTimeout=30000 --hookTimeout=30000 --retry=2"
)


class GOOSE_6804_TO_6804_ImageBase(Image):
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
        # ui/desktop/package.json engines: node ^24.10.0, npm ^11.6.1; the repo's
        # hermit pin is bin/.node-24.10.0.pkg. Official multi-arch image.
        return "node:24.10.0-bookworm"

    def image_tag(self) -> str:
        # SHARED base: toolchain + FULL-HISTORY clone, pinned to nothing. The
        # checkout and the history prune belong to the PR layer, so one base can
        # serve every PR of the repo.
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # DEBIAN_FRONTEND / LANG / proxy + CA ARGs and ENV, and ARG REPO_URL /
        # BASE_COMMIT are injected by DockerfileEnhancer right after FROM, so they
        # are not repeated here. build_dataset.py passes REPO_URL / BASE_COMMIT.
        #
        # SHARED base: NO checkout and NO history scrub here, and ${BASE_COMMIT}
        # is never consumed -- pinning this image to one PR's commit would prune
        # away every other PR's base commit. The clone keeps full history so any
        # PR layer can reach its own commit. Note the clone is written so that the
        # literal tokens "git clone" / "git fetch" never appear on one line, which
        # is what keeps DockerfileEnhancer from rewriting it into a pinned,
        # history-scrubbed fetch (image.py:356-435).
        return f"""FROM {image_name}

{self.global_env}

ENV ELECTRON_SKIP_BINARY_DOWNLOAD=1
ENV HUSKY=0
WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# The full-history clone is ~1 GB; a single dropped TLS stream ("curl 56 ... early
# EOF") must not fail the build, so retry a few times from a clean directory.
RUN set -eu; \\
    for attempt in 1 2 3 4; do \\
        if git -c http.version=HTTP/1.1 \\
            clone "${{REPO_URL}}" /home/{self.pr.repo}; then break; fi; \\
        rm -rf /home/{self.pr.repo}; \\
        if [ "$attempt" = 4 ]; then echo "clone failed after $attempt attempts" >&2; exit 1; fi; \\
        sleep $((attempt * 10)); \\
    done; \\
    git -C /home/{self.pr.repo} rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class GOOSE_6804_TO_6804_ImageDefault(Image):
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
        return GOOSE_6804_TO_6804_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
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
set -eo pipefail
export CI=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export HUSKY=0

# ---- Section 1: PIN ----
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# ---- Section 2: PROVISION ----
# npm ci installs exactly this commit's package-lock.json at build time.
# ELECTRON_SKIP_BINARY_DOWNLOAD: the Electron binary is not needed by the Vitest
# (jsdom) unit tests. HUSKY=0: skip git-hook installation from the "prepare" script.
cd /home/{repo}/{ui_dir}
npm ci --no-audit --no-fund

# ---- Section 3: GATE ----
# Resolves the test runner and the test-only deps (jsdom environment, React Testing Library); not tolerant.
./node_modules/.bin/vitest --version && node -e "require('jsdom'); require('@testing-library/react'); console.log('DEPS_OK')"
""".format(repo=self.pr.repo, sha=self.pr.base.sha, ui_dir=_UI_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}/{ui_dir}
{test_cmd}
""".format(repo=self.pr.repo, ui_dir=_UI_DIR, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
cd /home/{repo}/{ui_dir}
{test_cmd}
""".format(repo=self.pr.repo, ui_dir=_UI_DIR, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
cd /home/{repo}/{ui_dir}
{test_cmd}
""".format(repo=self.pr.repo, ui_dir=_UI_DIR, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # PR image: never enhanced (dependency() is an Image), so everything this
        # layer needs is written out here. The base is SHARED and pinned to
        # nothing, so THIS layer owns the pin and the history prune; the CA/proxy
        # ENV block is inherited from the base via FROM.
        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard
RUN git checkout {self.pr.base.sha}

RUN set -eux; \\
    git checkout --detach "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{self.pr.base.sha}")"; \\
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

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("aaif-goose", "goose_6804_to_6804")
class GOOSE_6804_TO_6804(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GOOSE_6804_TO_6804_ImageDefault(self.pr, self._config)

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
        test_log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Vitest verbose reporter, one line per test (full describe path kept):
        #   ✓ src/App.test.tsx > App Component > renders 122ms
        #   × src/a/B.test.tsx > B > Model selection > allows selecting 90ms
        #   ↓ src/OllamaSetup.test.tsx > OllamaSetup > when ... > handles failure
        # A file that fails to load shows up only as "FAIL  <file> [ <file> ]".
        # Duration / retry suffixes are outside the capture group, so a test has
        # the same name in every stage.
        re_test = re.compile(
            r"^\s*(✓|√|×|✗|↓)\s+(\S.*?)"
            r"(?:\s+\d+(?:\.\d+)?\s*m?s)?(?:\s+\(retry x\d+\))?\s*$"
        )
        re_file_fail = re.compile(r"^\s*FAIL\s+(\S+?\.(?:test|spec)\.[jt]sx?)\s+\[\s*\S+\s*\]\s*$")

        for line in test_log.splitlines():
            match = re_test.match(line)
            if match:
                status, name = match.group(1), match.group(2)
                if " > " not in name:
                    # file-level summary lines ("✓ src/x.test.tsx (12 tests)") are not tests
                    continue
                if status in ("✓", "√"):
                    passed_tests.add(name)
                elif status in ("×", "✗"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue
            match = re_file_fail.match(line)
            if match:
                failed_tests.add(match.group(1))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
