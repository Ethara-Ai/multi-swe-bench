import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# go.mod declares `go 1.15` and CI (.github/workflows/workflow.yml) builds on
# 1.15/1.16/1.19. 1.19 is the newest toolchain the repo is verified against and
# is new enough for its dependencies (golang.org/x/sys v0.3.0 needs >= 1.17),
# so it satisfies both ends without relying on newer-toolchain compat mode.
_GO_VERSION = "1.19"


class UgoImageBase(Image):
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
        return f"golang:{_GO_VERSION}"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class UgoImageDefault(Image):
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
        return UgoImageBase(self.pr, self.config)

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
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Must match the run scripts so the warmed build cache is actually reused.
export CGO_ENABLED=0

go test -v -count=1 ./... || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CGO_ENABLED=0
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CGO_ENABLED=0
git apply --whitespace=nowarn /home/test.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CGO_ENABLED=0
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
go test -v -count=1 ./...

""".format(pr=self.pr),
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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RE_PASS = re.compile(r"^--- PASS:\s+(\S+)")
_RE_FAIL = re.compile(r"^--- FAIL:\s+(\S+)")
_RE_SKIP = re.compile(r"^--- SKIP:\s+(\S+)")

_RE_PKG_OK = re.compile(r"^ok\s+(\S+)")
_RE_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)")
_RE_PKG_NOTEST = re.compile(r"^\?\s+(\S+)")

# Two subtests in the root package derive their name from a formatted Go value
# that contains a function pointer, e.g.
#   TestToObject/func(...ugo.Object)_(ugo.Object,_error):0x649960
# The address is fresh on every process, so the same test would carry a
# different name in each of the three stages: Report unions names across
# stages, so the run-stage name would be orphaned and the fix-stage name
# would be misclassified NONE->PASS. Mask the address to keep the name stable.
_RE_ADDR = re.compile(r"0x[0-9a-fA-F]+")


def _normalize(name: str) -> str:
    return _RE_ADDR.sub("0xADDR", name)


def ugo_parse_log(test_log: str) -> TestResult:
    """Parse `go test -v ./...` output, qualifying names with their package.

    ugo test names are not globally unique: `TestScript` exists in four
    packages (stdlib/fmt, stdlib/json, stdlib/strings, stdlib/time). Emitting
    the bare name would merge unrelated tests into one entry, so outcomes are
    buffered per package and flushed as `pkg::name` when the trailing
    `ok|FAIL|? pkg` summary line arrives.

    Names are also run through `_normalize()` to strip per-process pointer
    values, which would otherwise differ between stages.
    """
    test_log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # (status, raw_name) for the package currently being reported.
    buf: list[tuple[str, str]] = []

    def flush(pkg: str) -> None:
        for status, name in buf:
            qualified = f"{pkg}::{_normalize(name)}"
            if status == "PASS":
                if qualified not in failed_tests:
                    skipped_tests.discard(qualified)
                    passed_tests.add(qualified)
            elif status == "FAIL":
                passed_tests.discard(qualified)
                skipped_tests.discard(qualified)
                failed_tests.add(qualified)
            elif status == "SKIP":
                if qualified not in passed_tests and qualified not in failed_tests:
                    skipped_tests.add(qualified)
        buf.clear()

    for line in test_log.splitlines():
        # Subtest results are indented; strip so the anchored patterns match.
        line = line.strip()

        m = _RE_PASS.match(line)
        if m:
            buf.append(("PASS", m.group(1)))
            continue
        m = _RE_FAIL.match(line)
        if m:
            buf.append(("FAIL", m.group(1)))
            continue
        m = _RE_SKIP.match(line)
        if m:
            buf.append(("SKIP", m.group(1)))
            continue

        m = (
            _RE_PKG_OK.match(line)
            or _RE_PKG_FAIL.match(line)
            or _RE_PKG_NOTEST.match(line)
        )
        if m:
            flush(m.group(1))

    # No package summary line (build failure, or a panic that killed the run):
    # still record what was seen so the stage is not silently empty.
    if buf:
        flush("<unknown-package>")

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("ozanh", "ugo")
class Ugo(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return UgoImageDefault(self.pr, self._config)

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
        return ugo_parse_log(test_log)


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Delivered records carry a dash-joined number_interval, which Instance.create
# prefers over the bare repo key; the bare "ugo" key above still routes the
# build-time dataset, whose number_interval is empty.
_BUNDLE_NIS_OZANH = [
    "20",  # pr-20 (1 PR)
]

for _ni in _BUNDLE_NIS_OZANH:
    Instance.register("ozanh", _ni)(Ugo)
