import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "PrimeIntellect-ai"
_INTERVAL = "prime_agent_3556_to_3556"
_BASE_IMAGE = "rust:1.98.1-bookworm"
_REPO_DIR = "prime-agent"
_JOBS = "4"

_BEGIN = "===== MSB TEST RESULTS BEGIN ====="
_END = "===== MSB TEST RESULTS END ====="
_TARGET_PREFIX = "MSB_TARGET "

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

_ENV = """export CARGO_TERM_COLOR=never
export CARGO_BUILD_JOBS=[[JOBS]]
export CARGO_INCREMENTAL=0
export RUST_BACKTRACE=0
"""

_TARGETS_FN = r"""msb_targets() {
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      *.rs) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    d=$(dirname "$f")
    pkgdir=""
    while :; do
      if [ -f "$d/Cargo.toml" ] && grep -q '^\[package\]' "$d/Cargo.toml"; then
        pkgdir="$d"
        break
      fi
      [ "$d" = "." ] && break
      d=$(dirname "$d")
    done
    [ -n "$pkgdir" ] || continue
    pkg=$(sed -n '/^\[package\]/,/^\[/{s/^name *= *"\([^"]*\)".*/\1/p;}' "$pkgdir/Cargo.toml" | head -1)
    [ -n "$pkg" ] || continue
    if [ "$pkgdir" = "." ]; then rel="$f"; else rel="${f#$pkgdir/}"; fi
    case "$rel" in
      tests/*/main.rs) t="${rel#tests/}"; echo "$pkg --test ${t%/main.rs}" ;;
      tests/*/*) ;;
      tests/*.rs) t="${rel#tests/}"; echo "$pkg --test ${t%.rs}" ;;
      src/main.rs|src/bin/*) echo "$pkg --bins" ;;
      src/*) if [ -f "$pkgdir/src/lib.rs" ]; then echo "$pkg --lib"; else echo "$pkg --bins"; fi ;;
    esac
  done | sort -u
}
"""

_RETRY_FN = r"""msb_net_retry() {
  local n=0 out rc
  while :; do
    out=$("$@" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out"
    [ "$rc" -eq 0 ] && return 0
    n=$((n + 1))
    if [ "$n" -ge 5 ] || ! printf '%s' "$out" | grep -qiE 'could not resolve|timed out|timeout|connection (reset|refused|closed)|network|temporary failure|spurious|ssl|tls|early eof|unexpected disconnect|50[234]|failed to download|failed to fetch|curl'; then
      return "$rc"
    fi
    sleep $((n * 15))
  done
}
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO_DIR]]
[[ENV]]export CARGO_NET_OFFLINE=true
[[TARGETS_FN]]
mapfile -t TARGETS < <(msb_targets)
rc=0
echo "[[BEGIN]]"
for t in "${TARGETS[@]}"; do
  read -r pkg kind name <<< "$t"
  echo "[[TARGET_PREFIX]]$pkg/${name:-${kind#--}}"
  cargo test --offline -p "$pkg" $kind $name --no-fail-fast 2>&1 || rc=$?
done
echo "[[END]]"
exit "$rc"
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
[[ENV]][[RETRY_FN]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  msb_net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
  msb_net_retry rustup toolchain install
fi
RV=$(sed -n '/^\[\(workspace\.\)\{0,1\}package\]/,/^\[/{s/^rust-version *= *"\([^"]*\)".*/\1/p;}' Cargo.toml | head -1)
if [ -n "$RV" ]; then
  CUR=$(rustc --version | awk '{print $2}')
  if [ "$(printf '%s\n%s\n' "$RV" "$CUR" | sort -V | head -1)" != "$RV" ]; then
    msb_net_retry rustup toolchain install "$RV" --profile minimal
    rustup default "$RV"
  fi
fi
rustc --version
cargo --version

[[TARGETS_FN]]
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
msb_net_retry cargo fetch
mapfile -t TARGETS < <(msb_targets)
for t in "${TARGETS[@]}"; do
  read -r pkg kind name <<< "$t"
  cargo test -p "$pkg" $kind $name --no-run
done
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

msb_net_retry cargo fetch
mapfile -t TARGETS < <(msb_targets)
for t in "${TARGETS[@]}"; do
  read -r pkg kind name <<< "$t"
  cargo test -p "$pkg" $kind $name --no-run
done
bash /home/check_git_changes.sh

"""


def _render(template: str) -> str:
    return (
        template.replace("[[ENV]]", _ENV)
        .replace("[[TARGETS_FN]]", _TARGETS_FN)
        .replace("[[RETRY_FN]]", _RETRY_FN)
        .replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[JOBS]]", _JOBS)
        .replace("[[BEGIN]]", _BEGIN)
        .replace("[[END]]", _END)
        .replace("[[TARGET_PREFIX]]", _TARGET_PREFIX)
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class PrimeAgent3556To3556ImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _base_tag(self.pr)

    def workdir(self) -> str:
        return _base_tag(self.pr)

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


class PrimeAgent3556To3556ImageDefault(Image):
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
        return PrimeAgent3556To3556ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        prepare = (
            _render(_PREPARE_SH)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace(
                "[[REPO_URL]]", f"https://github.com/{self.pr.org}/{self.pr.repo}.git"
            )
        )

        apply_and_run = """#!/bin/bash

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", _render(_RUN_SH)),
            File(
                ".",
                "test-run.sh",
                apply_and_run.format(repo=_REPO_DIR, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                apply_and_run.format(
                    repo=_REPO_DIR, patches="/home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register(_ORG, _INTERVAL)
class PrimeAgent3556To3556(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PrimeAgent3556To3556ImageDefault(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        start = test_log.find(_BEGIN)
        end = test_log.rfind(_END)
        if start == -1:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )
        body = test_log[start + len(_BEGIN) : end if end > start else len(test_log)]

        re_result = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
        target = ""
        for raw in body.splitlines():
            line = raw.strip()
            if line.startswith(_TARGET_PREFIX):
                target = line[len(_TARGET_PREFIX) :].strip()
                continue
            m = re_result.match(line)
            if not m:
                continue
            name = f"{target}::{m.group(1)}" if target else m.group(1)
            status = m.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
