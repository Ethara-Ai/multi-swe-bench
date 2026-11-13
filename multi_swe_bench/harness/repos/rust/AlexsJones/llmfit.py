"""AlexsJones/llmfit -- Rust workspace (llmfit-core, llmfit-tui, llmfit-desktop).

Per-PR base (`base-pr-<N>`): the inherited Image.dockerfile() clones, pins
${BASE_COMMIT} and runs the hardening scrub; DockerfileEnhancer adds the
BuildKit directive, ARGs, proxy/TLS env and cert links.

Toolchain: rust 1.95.0. The repo ships no rust-toolchain.toml and no
`rust-version`; CI uses `stable`, and the repo's own Dockerfile states that
rustc >= 1.95 is required because sysinfo 0.39.x (pinned in Cargo.lock) bumped
its MSRV to 1.95. The official rust image is multi-arch (amd64 + arm64).

Scope: the workspace `default-members` (llmfit-core, llmfit-tui), which is what
CI's plain `cargo test` covers. llmfit-desktop (Tauri, GTK/WebKit) and the web
UI are not needed by the tests: llmfit-tui's build.rs falls back to a
placeholder dashboard when llmfit-web/dist is absent.
"""

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.95.0-bookworm"

_ENV = """export CI=true
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_NET_RETRY=10
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=1
export NO_COLOR=1"""

_TEST_CMD = "cargo test --locked --no-fail-fast"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

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
"""


class LlmfitImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["pkg-config"]

    # dockerfile() is inherited: Image.dockerfile() emits FROM -> apt -> clone
    # -> reset -> checkout ${BASE_COMMIT} -> hardening scrub (4 assertions).


class LlmfitImageDefault(Image):
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
        return LlmfitImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

# Section 1 -- PIN
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

# Section 2 -- PROVISION (fetch the locked dependency set, retried for
# transient network errors, then pre-compile every test target)
fetched=0
for attempt in 1 2 3 4 5; do
  if cargo fetch --locked; then
    fetched=1
    break
  fi
  sleep 20
done
test "$fetched" = 1

# Section 3 -- GATE (non-tolerant): HEAD is the base commit and every test
# target of the default workspace members compiles.
test "$(git rev-parse HEAD)" = "{sha}"
{_TEST_CMD} --no-run
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
{_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {base.image_full_name()}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("AlexsJones", "llmfit")
class Llmfit(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LlmfitImageDefault(self.pr, self._config)

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
        """Names are `<artifact>:<source> > <test>`.

        libtest names are only unique within one test binary, so each result
        is qualified by the preceding `Running` / `Doc-tests` line. The
        artifact's per-build hash suffix and doc-test line numbers are
        dropped so a name is identical across the three stages.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", test_log)
        running = re.compile(r"^Running (?:unittests )?(\S+) \((?:.*/)?([^/()]+?)(?:-[0-9a-f]{8,})?(?:\.exe)?\)")
        doc_tests = re.compile(r"^Doc-tests (\S+)")
        result = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)\b")
        doc_line = re.compile(r"\s+\(line \d+\)$")

        target = ""
        for raw in log.splitlines():
            line = raw.strip()
            found = running.match(line)
            if found:
                target = f"{found.group(2)}:{found.group(1)}"
                continue
            found = doc_tests.match(line)
            if found:
                target = f"{found.group(1)}:doc"
                continue
            found = result.match(line)
            if not found or not target:
                continue
            name = f"{target} > {doc_line.sub('', found.group(1))}"
            status = found.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
