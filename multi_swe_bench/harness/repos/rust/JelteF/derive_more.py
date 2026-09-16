import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_CARGO_FEATURES = "full,testing-helpers"

_TEST_BODY = """\
export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1

CARGO_ARGS="--workspace --features @FEATURES@ --no-fail-fast"
OUT=/tmp/suite.out
rm -f "$OUT"

set +e
cargo test $CARGO_ARGS --lib >> "$OUT" 2>&1
cargo test $CARGO_ARGS --doc >> "$OUT" 2>&1
for _t in $(awk '/^\\[\\[test\\]\\]/{f=1;next} f && /^name = /{gsub(/^name = "|"$/,"",$0); print; f=0}' Cargo.toml); do
    cargo test $CARGO_ARGS --test "$_t" >> "$OUT" 2>&1
done
set -e

cat "$OUT"

grep -q "^test result:" "$OUT"
""".replace("@FEATURES@", _CARGO_FEATURES)


class DeriveMoreImageBase(Image):
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
        return "rust:1.71"

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

RUN rustup toolchain install nightly --profile minimal

{code}

{self.clear_env}

"""


class DeriveMoreImageDefault(Image):
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
        return DeriveMoreImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export CARGO_TERM_COLOR=never
export RUST_BACKTRACE=1

cargo +nightly update -Z minimal-versions || cargo generate-lockfile || true

cargo fetch || true
cargo build --workspace --features {features} --tests || true

""".format(pr=self.pr, features=_CARGO_FEATURES),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_RUNNING_LINE = re.compile(
    r"^\s*Running\s+(?:unittests\s+)?(?P<target>\S+?)\s*\(target[^)]*\)\s*$"
)
_DOCTEST_LINE = re.compile(r"^\s*Doc-tests\s+(?P<crate>\S+)\s*$")
_RUNNING_COUNT = re.compile(r"^running \d+ tests?$")
_CASE_LINE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+"
    r"(?P<status>ok|FAILED|failed|mismatch|error|ignored|wip)\b.*$"
)
_RESULT_LINE = re.compile(r"^test result:")
_DOCTEST_LINENO = re.compile(r"\s*\(line\s+\d+\)")


def parse_libtest_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    current_target = ""
    pending_target = ""
    seen_counts: dict[str, int] = {}

    def qualify(name: str) -> str:
        full = f"{current_target}::{name}" if current_target else name
        count = seen_counts.get(full, 0) + 1
        seen_counts[full] = count
        return full if count == 1 else f"{full} #{count}"

    for raw in clean.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RUNNING_LINE.match(line)
        if m:
            pending_target = m.group("target")
            continue

        m = _DOCTEST_LINE.match(line)
        if m:
            pending_target = f"Doc-tests {m.group('crate')}"
            continue

        if _RUNNING_COUNT.match(line.strip()):
            if pending_target:
                current_target = pending_target
                pending_target = ""
            continue

        if _RESULT_LINE.match(line):
            continue

        m = _CASE_LINE.match(line)
        if m:
            name = _DOCTEST_LINENO.sub("", m.group("name").strip())
            if not name:
                continue
            status = m.group("status").lower()
            test_name = qualify(name)
            if status == "ok":
                passed_tests.add(test_name)
            elif status in ("failed", "mismatch", "error"):
                failed_tests.add(test_name)
            else:
                skipped_tests.add(test_name)

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


@Instance.register("JelteF", "derive_more")
class DeriveMore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return DeriveMoreImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_libtest_log(log)
