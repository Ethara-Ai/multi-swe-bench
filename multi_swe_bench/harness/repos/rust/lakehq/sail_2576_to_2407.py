import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "lakehq"
_INTERVAL = "sail_2576_to_2407"
_BASE_IMAGE = "rust:1.99.0-bookworm"
_REPO_DIR = "sail"
_JOBS = "4"

_REPORT_BEGIN = "===== MSB REPORT BEGIN ====="
_REPORT_END = "===== MSB REPORT END ====="
_CASE_RE = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

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

_COMMON = r"""export CARGO_BUILD_JOBS=[[JOBS]]
export CARGO_TERM_COLOR=never
export CARGO_NET_RETRY=10
export CARGO_HTTP_TIMEOUT=60
export CARGO_HTTP_MULTIPLEXING=false
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0
export RUSTFLAGS="-C link-arg=-fuse-ld=lld"
export RUST_BACKTRACE=0
export VENV=/opt/msb-venv

net_retry() {
  local n=0 rc out
  while :; do
    out=$(timeout 1800 "$@" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out"
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 124 ] && out="$out timed out"
    if [ "$n" -lt 6 ] && printf '%s' "$out" | grep -qiE 'could not resolve|resolve host|timed out|timeout|connection (reset|refused|closed)|network|spurious|failed to download|early eof|unexpected eof|tls|ssl|http2|502|503|504|429|temporary failure'; then
      n=$((n + 1))
      echo "net_retry: network error, retry $n" >&2
      sleep $((n * 10))
      continue
    fi
    return "$rc"
  done
}

bdd_module_of() {
  local d f
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    for f in "$d"/test_*.py; do
      [ -f "$f" ] || continue
      if grep -q 'scenarios(' "$f"; then
        echo "$f"
        return 0
      fi
    done
    case "$d" in
      */tests) return 1 ;;
    esac
    d=$(dirname "$d")
  done
  return 1
}

compute_targets() {
  local f d rel feat mod
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    feat=""
    mod=""
    case "$f" in
      */tests/*/__snapshots__/*)
        d="${f%%/__snapshots__/*}"
        rel="${f#*/__snapshots__/}"
        case "$rel" in
          features/*) feat="$d/${rel%.*}.feature" ;;
          test_*) mod="$d/$(echo "$rel" | cut -d/ -f1)"; mod="${mod%.*}.py" ;;
        esac
        ;;
      */tests/*.feature) feat="$f" ;;
      */tests/*/test_*.py|*/tests/test_*.py) mod="$f" ;;
    esac
    if [ -n "$feat" ] && [ -f "$feat" ]; then
      mod=$(bdd_module_of "$feat") || continue
      echo "$mod|tests/${feat#*/tests/}"
    elif [ -n "$mod" ] && [ -f "$mod" ]; then
      echo "$mod|"
    fi
  done | sort -u
}
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO_DIR]]
[[COMMON]]
export CARGO_NET_OFFLINE=true
export PIP_NO_INDEX=1
. "$VENV/bin/activate"
(while sleep 120; do echo "run: heartbeat $(date +%T)"; done) &
HB=$!
trap 'kill "$HB" 2>/dev/null' EXIT

TARGETS=$(compute_targets)
if [ -z "$TARGETS" ]; then
  echo "run: no test.patch targets present at this commit"
  exit 0
fi
echo "run: targets:"
echo "$TARGETS"
maturin develop --skip-install < /dev/null
brc=$?
if [ "$brc" -ne 0 ]; then
  echo "run: native module build failed rc=$brc" >&2
  exit "$brc"
fi
overall=0
for mod in $(echo "$TARGETS" | cut -d'|' -f1 | sort -u); do
  sels=$(echo "$TARGETS" | awk -F'|' -v m="$mod" '$1==m {print $2}')
  kexpr=""
  if ! echo "$sels" | grep -qx ''; then
    kexpr=$(echo "$sels" | paste -sd'|' - | sed 's/|/ or /g')
  fi
  rm -f /tmp/msb-junit.xml
  echo "===== MSB TARGET BEGIN $mod ====="
  if [ -n "$kexpr" ]; then
    python -m pytest -p no:cacheprovider -q "$mod" -k "$kexpr" --junitxml=/tmp/msb-junit.xml -o junit_logging=no < /dev/null
  else
    python -m pytest -p no:cacheprovider -q "$mod" --junitxml=/tmp/msb-junit.xml -o junit_logging=no < /dev/null
  fi
  rc=$?
  echo "[[REPORT_BEGIN]]"
  if [ -f /tmp/msb-junit.xml ]; then
    cat /tmp/msb-junit.xml
    echo
  fi
  echo "[[REPORT_END]]"
  echo "===== MSB TARGET END $mod rc=$rc ====="
  [ "$rc" -eq 0 ] || overall=$rc
done
exit "$overall"
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO_DIR]]
[[COMMON]]
(while sleep 120; do echo "prepare: heartbeat $(date +%T)"; done) &
HB=$!
trap 'kill "$HB" 2>/dev/null' EXIT

git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  net_retry git fetch --no-tags [[REPO_URL]] [[SHA]]
fi
git checkout [[SHA]]
if [ -f .gitmodules ]; then
  net_retry git submodule update --init --recursive
fi
bash /home/check_git_changes.sh

if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
  net_retry rustup toolchain install
else
  RV=$(awk '/^\[(workspace\.)?package\]/{f=1;next} /^\[/{f=0} f && $1=="rust-version" {sub(/^[^=]*=[ \t]*"/,"");sub(/".*$/,"");print;exit}' Cargo.toml)
  CUR=$(rustc --version | awk '{print $2}')
  if [ -n "$RV" ] && [ "$(printf '%s\n%s\n' "$RV" "$CUR" | sort -V | tail -n1)" != "$CUR" ]; then
    net_retry rustup toolchain install "$RV" --profile minimal
    rustup override set "$RV"
  fi
fi
net_retry rustup component add rustfmt
rustc --version
cargo --version

net_retry apt-get update
net_retry apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev lld python3 python3-venv python3-dev
rm -rf /var/lib/apt/lists/*
protoc --version
python3 --version

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
. "$VENV/bin/activate"
python -c '
import tomllib
with open("pyproject.toml", "rb") as fh:
    data = tomllib.load(fh)
for req in data.get("build-system", {}).get("requires", []):
    print(req)
for req in data.get("project", {}).get("optional-dependencies", {}).get("test", []):
    print(req)
' > /tmp/msb-requirements.txt
cat /tmp/msb-requirements.txt
net_retry python -m pip install --upgrade pip
CUTOFF=$(git log -1 --format=%cI HEAD)
echo "prepare: python deps uploaded prior to $CUTOFF"
net_retry python -m pip install --uploaded-prior-to "$CUTOFF" -r /tmp/msb-requirements.txt
rm -f /tmp/msb-requirements.txt

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
net_retry cargo fetch
git apply -R --whitespace=nowarn /home/fix.patch
git apply -R --whitespace=nowarn /home/test.patch
bash /home/check_git_changes.sh

export CARGO_NET_OFFLINE=true
PIP_NO_INDEX=1 maturin develop < /dev/null
python -c "import pysail._native"
git apply --whitespace=nowarn /home/test.patch
echo "prepare: targets:"
compute_targets
git apply -R --whitespace=nowarn /home/test.patch
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[COMMON]]", _COMMON)
        .replace("[[JOBS]]", _JOBS)
        .replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[REPORT_BEGIN]]", _REPORT_BEGIN)
        .replace("[[REPORT_END]]", _REPORT_END)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Sail2576To2407ImageBase(Image):
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


class Sail2576To2407ImageDefault(Image):
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
        return Sail2576To2407ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
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
class Sail2576To2407(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Sail2576To2407ImageDefault(self.pr, self._config)

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

        start = test_log.find(_REPORT_BEGIN)
        while start != -1:
            end = test_log.find(_REPORT_END, start)
            if end == -1:
                break
            report = test_log[start + len(_REPORT_BEGIN) : end]
            for m in _CASE_RE.finditer(report):
                attrs = dict(_ATTR_RE.findall(m.group(1)))
                name = f"{attrs.get('classname', '')}::{attrs.get('name', '')}"
                body = m.group(3) or ""
                if "<failure" in body or "<error" in body:
                    failed_tests.add(name)
                elif "<skipped" in body:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)
            start = test_log.find(_REPORT_BEGIN, end)

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
