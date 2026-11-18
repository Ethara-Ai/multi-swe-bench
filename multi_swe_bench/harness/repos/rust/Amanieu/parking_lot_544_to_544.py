import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "Amanieu"
_INTERVAL = "parking_lot_544_to_544"
_BASE_IMAGE = "rust:1.97-bookworm"
_SYSTEM_PACKAGES = ""
_CARGO_TEST_ARGS = "--all-features"

_REPORTS_BEGIN = "===== TEST REPORTS BEGIN ====="
_REPORTS_END = "===== TEST REPORTS END ====="
_TARGET_MARK = "MSB_TARGET"

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

_CARGO_ENV = r"""export CARGO_TERM_COLOR=never
export CARGO_BUILD_JOBS=4
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0
export CARGO_PROFILE_TEST_DEBUG=0
export RUST_BACKTRACE=0
"""

_HELPERS = r"""pkg_dir() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    if [ -f "$d/Cargo.toml" ] && grep -q '^\[package\]' "$d/Cargo.toml"; then
      echo "$d"
      return
    fi
    d=$(dirname "$d")
  done
  if [ -f Cargo.toml ] && grep -q '^\[package\]' Cargo.toml; then
    echo "."
  fi
}

crate_name() {
  awk '/^\[package\]/{p=1;next} /^\[/{p=0} p&&/^name *=/{gsub(/^name *= *"|".*$/,"");gsub(/-/,"_");print;exit}' "$1/Cargo.toml"
}

has_lib() {
  [ -f "$1/src/lib.rs" ]
}

has_bins() {
  [ -f "$1/src/main.rs" ] || [ -d "$1/src/bin" ]
}

msb_targets() {
  local f d rel t
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      *.rs) ;;
      *) continue ;;
    esac
    d=$(pkg_dir "$f")
    [ -n "$d" ] || continue
    if [ "$d" = "." ]; then rel="$f"; else rel="${f#$d/}"; fi
    case "$rel" in
      tests/*/*)
        t="${rel#tests/}"
        t="${t%%/*}"
        if [ -f "$d/tests/$t/main.rs" ]; then echo "$d --test $t"; fi
        ;;
      tests/*.rs)
        t="${rel#tests/}"
        t="${t%.rs}"
        if [ -f "$d/tests/$t.rs" ]; then echo "$d --test $t"; fi
        ;;
      src/*)
        if has_lib "$d"; then echo "$d --lib"; fi
        if has_bins "$d"; then echo "$d --bins"; fi
        ;;
    esac
  done | sort -u
}

msb_packages() {
  local f d
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      *.rs) ;;
      *) continue ;;
    esac
    d=$(pkg_dir "$f")
    if [ -n "$d" ]; then echo "$d"; fi
  done | sort -u
}
"""

_NET_RETRY = r"""NET_ERR_RE='transfer too slow|Could not resolve|Couldn.t resolve|Temporary failure|Connection (reset|refused|timed out)|Operation timed out|timed out|spurious network|failed to download|failed to fetch|failed to get|network failure|error sending request|unexpected EOF|early EOF|SSL|TLS|50[234] |Hash Sum mismatch|Failed to fetch|RPC failed'

net_retry() {
  local n=0
  local log=/tmp/msb_net_retry.log
  local status
  while true; do
    set +e
    if [ -n "${NET_RETRY_TIMEOUT:-}" ]; then
      timeout -k 60 "$NET_RETRY_TIMEOUT" "$@" 2>&1 | tee "$log"
    else
      "$@" 2>&1 | tee "$log"
    fi
    status=${PIPESTATUS[0]}
    set -e
    if [ "$status" -eq 0 ]; then
      return 0
    fi
    n=$((n + 1))
    if [ "$status" -ne 124 ] && ! grep -qE "$NET_ERR_RE" "$log"; then
      echo "prepare: deterministic failure: $*" >&2
      return 1
    fi
    if [ "$n" -ge 6 ]; then
      echo "prepare: network failure persisted: $*" >&2
      return 1
    fi
    echo "prepare: network error, retry $n: $*" >&2
    sleep $((n * 15))
  done
}
"""

_TOOLCHAIN = r"""if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
  if ! RUSTUP_AUTO_INSTALL=0 rustc --version > /dev/null 2>&1; then
    NET_RETRY_TIMEOUT=900 net_retry rustup toolchain install --profile minimal
  fi
else
  REQ=$(grep -hE '^rust-version *= *"' Cargo.toml $(msb_packages | sed 's|$|/Cargo.toml|') 2>/dev/null | sed -E 's/^rust-version *= *"([^"]+)".*/\1/' | sort -V | tail -n1)
  CUR=$(rustc --version | awk '{print $2}')
  if [ -n "$REQ" ] && [ "$(printf '%s\n%s\n' "$REQ" "$CUR" | sort -V | head -n1)" != "$REQ" ]; then
    NET_RETRY_TIMEOUT=900 net_retry rustup toolchain install "$REQ" --profile minimal
    rustup default "$REQ"
  fi
fi
rustc --version
cargo --version
"""

_SYSTEM_DEPS = r"""MISSING=""
for p in [[PKGS]]; do
  dpkg -s "$p" > /dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  net_retry apt-get update
  net_retry apt-get install -y --no-install-recommends $MISSING
  rm -rf /var/lib/apt/lists/*
fi
"""

_PREPARE = r"""#!/bin/bash
set -e
set -o pipefail

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
export CARGO_NET_RETRY=10
export CARGO_HTTP_TIMEOUT=120
export CARGO_HTTP_MULTIPLEXING=false
[[NET_RETRY]]
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
bash /home/check_git_changes.sh

[[SYSTEM_DEPS]]
[[CARGO_ENV]]
[[HELPERS]]
[[TOOLCHAIN]]
net_retry cargo fetch
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
net_retry cargo fetch
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

export CARGO_NET_OFFLINE=true
for d in $(msb_packages); do
  if has_lib "$d"; then
    net_retry cargo test --no-run --manifest-path "$d/Cargo.toml" [[TEST_ARGS]] --lib
  fi
  if has_bins "$d"; then
    net_retry cargo test --no-run --manifest-path "$d/Cargo.toml" [[TEST_ARGS]] --bins
  fi
done
msb_targets | while read -r d flag name; do
  net_retry cargo test --no-run --manifest-path "$d/Cargo.toml" [[TEST_ARGS]] $flag $name < /dev/null
done
bash /home/check_git_changes.sh

"""

_RUN = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO]]
[[CARGO_ENV]]
export CARGO_NET_OFFLINE=true
[[HELPERS]]
TARGETS=$(msb_targets)
echo "MSB_TEST_TARGETS: $(echo $TARGETS)"
rc=0
echo "[[BEGIN]]"
while read -r d flag name; do
  [ -n "$d" ] || continue
  crate=$(crate_name "$d")
  case "$flag" in
    --lib) label="$crate" ;;
    --bins) label="$crate::bin" ;;
    *) label="$crate::$name" ;;
  esac
  echo "[[MARK]] $label"
  cargo test --manifest-path "$d/Cargo.toml" [[TEST_ARGS]] $flag $name --no-fail-fast < /dev/null 2>&1
  status=$?
  [ "$status" -eq 0 ] || rc=$status
done <<< "$TARGETS"
echo "[[END]]"
exit "$rc"
"""

_APPLY_AND_RUN = """#!/bin/bash

cd /home/{repo}
if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/run.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[NET_RETRY]]", _NET_RETRY)
        .replace("[[SYSTEM_DEPS]]", _SYSTEM_DEPS)
        .replace("[[PKGS]]", _SYSTEM_PACKAGES)
        .replace("[[TEST_ARGS]]", _CARGO_TEST_ARGS)
        .replace("[[CARGO_ENV]]", _CARGO_ENV)
        .replace("[[HELPERS]]", _HELPERS)
        .replace("[[TOOLCHAIN]]", _TOOLCHAIN)
        .replace("[[BEGIN]]", _REPORTS_BEGIN)
        .replace("[[END]]", _REPORTS_END)
        .replace("[[MARK]]", _TARGET_MARK)
        .replace("[[REPO]]", pr.repo)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class ParkingLot544To544ImageBase(Image):
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


class ParkingLot544To544ImageDefault(Image):
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
        return ParkingLot544To544ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _render(_PREPARE, self.pr)),
            File(".", "run.sh", _render(_RUN, self.pr)),
            File(
                ".",
                "test-run.sh",
                _APPLY_AND_RUN.format(repo=repo, patches="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                _APPLY_AND_RUN.format(
                    repo=repo, patches="/home/test.patch /home/fix.patch"
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
class ParkingLot544To544(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ParkingLot544To544ImageDefault(self.pr, self._config)

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
        start = test_log.find(_REPORTS_BEGIN)
        end = test_log.rfind(_REPORTS_END)
        if start == -1:
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=passed_tests,
                failed_tests=failed_tests,
                skipped_tests=skipped_tests,
            )
        if end == -1 or end < start:
            end = len(test_log)
        section = test_log[start + len(_REPORTS_BEGIN) : end]

        re_result = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
        label = ""
        for raw in section.splitlines():
            line = raw.rstrip()
            if line.startswith(_TARGET_MARK + " "):
                label = line[len(_TARGET_MARK) + 1 :].strip()
                continue
            if not label:
                continue
            m = re_result.match(line.strip())
            if not m:
                continue
            name = f"{label}::{m.group(1)}"
            if m.group(2) == "ok":
                passed_tests.add(name)
            elif m.group(2) == "FAILED":
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
