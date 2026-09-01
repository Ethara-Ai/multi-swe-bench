import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_BARE_NAME_PRS = {28, 39}
_LEAF_KEY_PRS = {72}


class FlurryImageBase(Image):
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
        return "rust:1.98"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""

class FlurryImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return FlurryImageBase(self.pr, self.config)

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

export RUST_BACKTRACE=1

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha} 2>/dev/null || {{
    git fetch --depth=1 https://github.com/{pr.org}/{pr.repo}.git {pr.base.sha}
    git checkout FETCH_HEAD
}}
bash /home/check_git_changes.sh

cargo test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
# PRs 32 and 39 require crossbeam-epoch ^0.9 with the "sanitize" feature,
# which no published crate still provides. Applied here rather than in
# prepare.sh so the image itself ships a pristine tree. No-op elsewhere.
sed -i 's/^crossbeam-epoch = "0.9"$/crossbeam-epoch = "0.8"/' Cargo.toml

status=0
cargo test --lib || status=$?
cargo test --doc || status=$?
for f in tests/*.rs tests/*/main.rs; do
    [ -e "$f" ] || continue
    case "$f" in
        */main.rs) name=$(basename "$(dirname "$f")") ;;
        *)         name=$(basename "$f" .rs) ;;
    esac
    cargo test --test "$name" || status=$?
done
exit $status

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
# PRs 32 and 39 require crossbeam-epoch ^0.9 with the "sanitize" feature,
# which no published crate still provides. Applied here rather than in
# prepare.sh so the image itself ships a pristine tree. No-op elsewhere.
sed -i 's/^crossbeam-epoch = "0.9"$/crossbeam-epoch = "0.8"/' Cargo.toml

status=0
cargo test --lib || status=$?
cargo test --doc || status=$?
for f in tests/*.rs tests/*/main.rs; do
    [ -e "$f" ] || continue
    case "$f" in
        */main.rs) name=$(basename "$(dirname "$f")") ;;
        *)         name=$(basename "$f" .rs) ;;
    esac
    cargo test --test "$name" || status=$?
done
exit $status

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export RUST_BACKTRACE=1

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
# PRs 32 and 39 require crossbeam-epoch ^0.9 with the "sanitize" feature,
# which no published crate still provides. Applied here rather than in
# prepare.sh so the image itself ships a pristine tree. No-op elsewhere.
sed -i 's/^crossbeam-epoch = "0.9"$/crossbeam-epoch = "0.8"/' Cargo.toml

status=0
cargo test --lib || status=$?
cargo test --doc || status=$?
for f in tests/*.rs tests/*/main.rs; do
    [ -e "$f" ] || continue
    case "$f" in
        */main.rs) name=$(basename "$(dirname "$f")") ;;
        *)         name=$(basename "$f" .rs) ;;
    esac
    cargo test --test "$name" || status=$?
done
exit $status

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

@Instance.register("jonhoo", "flurry")
class Flurry(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return FlurryImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        unittest_re = re.compile(r"^\s*Running\s+unittests\s+\S+\s+\(")
        target_re = re.compile(r"^\s*Running\s+(\S+)\s+\(")
        doc_re = re.compile(r"^\s*Doc-tests\s+\S+")
        start_re = re.compile(r"^running \d+ tests?$")
        result_re = re.compile(
            r"^test\s+(.+?)(?:\s+-\s+should panic)?\s+\.\.\.\s+(ok|FAILED|ignored)\b"
        )
        line_no_re = re.compile(r"\s*\(line (\d+)\)")
        doc_path_re = re.compile(r"^(\S+\.rs) - (.*)$")

        lines = log.splitlines()

        targets: list[str] = []
        for raw in lines:
            line = raw.rstrip()
            if unittest_re.match(line) or doc_re.match(line):
                targets.append("")
                continue
            m = target_re.match(line)
            if m:
                targets.append(m.group(1))

        entries: list[tuple[str, str, Optional[int], str]] = []
        block = -1

        for raw in lines:
            line = raw.rstrip()

            if start_re.match(line):
                block += 1
                continue

            m = result_re.match(line)
            if not m:
                continue

            name, status = m.group(1), m.group(2)
            target = targets[block] if 0 <= block < len(targets) else ""
            doc_name = doc_path_re.match(name)
            if doc_name:
                target, name = "", doc_name.group(2)
            line_match = line_no_re.search(name)
            line_no = int(line_match.group(1)) if line_match else None
            entries.append((target, line_no_re.sub("", name), line_no, status))

        ranks: dict[tuple[str, str], dict[int, int]] = {}
        for target, name, line_no, _ in entries:
            if line_no is not None:
                ranks.setdefault((target, name), {})[line_no] = 0
        for group, group_lines in ranks.items():
            for rank, line_no in enumerate(sorted(group_lines), 1):
                group_lines[line_no] = rank

        for target, name, line_no, status in entries:
            if line_no is not None:
                name = f"{name} #{ranks[(target, name)][line_no]}"
            if self.pr.number in _LEAF_KEY_PRS and not target and "::" in name and " " not in name:
                name = name.split("::")[-1]
            if self.pr.number in _BARE_NAME_PRS:
                key = name
            else:
                key = f"{target}::{name}" if target else name
            if status == "ok":
                passed_tests.add(key)
            elif status == "FAILED":
                failed_tests.add(key)
            else:
                skipped_tests.add(key)

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
