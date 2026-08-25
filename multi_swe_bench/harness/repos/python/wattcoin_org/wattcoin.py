import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The full suite is run with --continue-on-collection-errors on purpose.
# At the base commit `skills/wattcoin/wattcoin.py` has a SyntaxError
# ("expected 'except' or 'finally' block"), so tests/test_wattcoin_skill.py and
# tests/test_wattcoin_node_earnings.py fail to import. Without the flag pytest
# aborts the whole session ("Interrupted: 2 errors during collection") and
# reports nothing at all; with it the other 60 tests still run and report.
PYTEST_CMD = (
    "python -m pytest tests/ -v --no-header -rA --tb=no "
    "-p no:cacheprovider --continue-on-collection-errors"
)


class ImageBase(Image):
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
        # requirements.txt needs flask>=3.0 and solders/solana wheels; the fix
        # patch also uses PEP 585 builtin generics in a module-level annotation
        # (`dict[tuple[str, int], ...]`), which is evaluated at import time and
        # so requires 3.9+. 3.11 has wheels for every pinned dependency.
        return "python:3.11-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Must NOT emit a `# syntax=...` directive or bake hardening inline:
        # DockerfileEnhancer.enhance() treats that directive as a sentinel
        # (image.py: `if SYNTAX_DIRECTIVE in raw: return raw`) and skips the
        # whole file, silently dropping proxy/CA-cert/git-scrub injection.
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The repo fetch is written in DockerfileEnhancer's OWN standardized
        # form on purpose. _standardize_repo_fetch() (image.py) rewrites
        #     RUN git clone <url> /home/<repo>      (and  COPY <repo> /home/<repo>)
        # into clone + `git checkout ${BASE_COMMIT}` + hardening, but its regex
        # carries a negative lookahead `(?!"\\$\\{REPO_URL\\}")` -- a clone already
        # in standardized form is left alone. We need that opt-out here because
        # PR #106's base commit is DANGLING: WattCoin-Org rewrote `main` after
        # the PR merged, so 90700943... is not reachable from any ref and a
        # plain clone cannot check it out ("fatal: unable to read tree").
        # `git fetch origin <sha>` still retrieves it from GitHub, so we clone,
        # fetch the sha explicitly, then reproduce the checkout + hardening the
        # enhancer would otherwise have appended.
        #
        # This also makes the `need_clone=False` case moot: the enhancer rewrites
        # COPY into a clone anyway, so both paths use the same fetch-aware form.
        return f"""FROM {image_name}

{self.global_env}

ENV CI=true

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git fetch origin ${{BASE_COMMIT}}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

CMD ["/bin/bash"]

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self.config)

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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# PR #106's base commit is dangling (main was rewritten after the merge), so a
# plain clone cannot reach it -- "fatal: unable to read tree". The base image
# already fetched it by sha, and the enhancer's hardening block then removed the
# `origin` remote, so fetch ONLY if the object is genuinely missing; otherwise
# `git fetch origin` would die with "'origin' does not appear to be a git
# repository".
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch origin {pr.base.sha}
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# `|| true` per harness convention: native/compiled wheels (solders is a Rust
# extension) can fail to build on arm64, and that must not abort the image
# build. A genuinely broken install still surfaces as collection errors in the
# smoke run below.
pip install --no-cache-dir -r requirements.txt || true

{pytest_cmd} || true

""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
{pytest_cmd}

""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{pytest_cmd}

""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{pytest_cmd}

""".format(pr=self.pr, pytest_cmd=PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("WattCoin-Org", "wattcoin")
class Wattcoin(Instance):
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

        # Strip ANSI colour codes FIRST. pytest emits them whenever stdout is a
        # TTY; without this they are captured *into* the test name, so the same
        # test can carry a different name in different stages and Report's
        # cross-stage union splits it into two entries.
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # Both patterns are anchored per line and matched against one line at a
        # time. An unanchored whole-log scan is the standard failure mode here:
        # in the `-rA` summary block the test name ending a PASSED line binds to
        # the status word starting the next line, putting the same test in both
        # passed and failed -- which TestResult.__post_init__ rejects outright.
        #
        # Progress form: tests/test_x.py::test_y PASSED   [ 50%]
        re_progress = re.compile(
            r"^(\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # Summary form: FAILED tests/test_x.py::test_y - AssertionError: ...
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )

        def record(status: str, name: str) -> None:
            # Requiring "::" keeps file-level collection errors
            # (`ERROR tests/test_wattcoin_skill.py`) out of the counts -- those
            # are import failures, not tests, and they are present identically
            # in the run/test/fix states.
            if "::" not in name:
                return
            if status == "PASSED":
                if name in failed_tests or name in skipped_tests:
                    return
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                passed_tests.discard(name)
                skipped_tests.discard(name)
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL", "XPASS"):
                if name in passed_tests or name in failed_tests:
                    return
                skipped_tests.add(name)

        for line in test_log.splitlines():
            line = line.strip()

            m = re_progress.match(line)
            if m:
                record(m.group(2), m.group(1))
                continue

            m = re_summary.match(line)
            if m:
                record(m.group(1), m.group(2))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
