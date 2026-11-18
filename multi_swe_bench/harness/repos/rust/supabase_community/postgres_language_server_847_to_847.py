import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "supabase-community"
_INTERVAL = "postgres_language_server_847_to_847"
_BASE_IMAGE = "rust:1.99.0-bookworm"
_REPO_DIR = "postgres-language-server"
_JOBS = "4"

_BEGIN_RE = re.compile(r"^===== MSB TARGET BEGIN (\S+) =====$")
_END_RE = re.compile(r"^===== MSB TARGET END (\S+) rc=\d+ =====$")
_TEST_RE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FAIL_RE = re.compile(r"^---- (\S+) stdout ----$")

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
export RUST_BACKTRACE=0

net_retry() {
  local n=0 rc out
  while :; do
    out=$(timeout 1800 "$@" 2>&1) && rc=0 || rc=$?
    printf '%s\n' "$out"
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 124 ] && out="$out timed out"
    if [ "$n" -lt 6 ] && printf '%s' "$out" | grep -qiE 'could not resolve|resolve host|timed out|timeout|connection (reset|refused|closed)|network|spurious|failed to download|early eof|unexpected eof|tls|ssl|http2|502|503|504|429'; then
      n=$((n + 1))
      echo "net_retry: network error, retry $n" >&2
      sleep $((n * 10))
      continue
    fi
    return "$rc"
  done
}

crate_dir_of() {
  local d
  d=$(dirname "$1")
  while [ "$d" != "." ] && [ "$d" != "/" ]; do
    if [ -f "$d/Cargo.toml" ] && grep -qE '^\[package\]' "$d/Cargo.toml"; then
      echo "$d"
      return 0
    fi
    d=$(dirname "$d")
  done
  if [ -f Cargo.toml ] && grep -qE '^\[package\]' Cargo.toml; then
    echo "."
    return 0
  fi
  return 1
}

pkg_name_of() {
  awk '/^\[package\]/{f=1;next} /^\[/{f=0} f && $1=="name" {sub(/^[^=]*=[ \t]*"/,"");sub(/".*$/,"");print;exit}' "$1/Cargo.toml"
}

all_test_targets() {
  local c=$1 t
  for t in "$c"/tests/*.rs "$c"/tests/*/main.rs; do
    [ -f "$t" ] || continue
    case "$t" in
      */main.rs) t=$(basename "$(dirname "$t")") ;;
      *) t=$(basename "$t" .rs) ;;
    esac
    echo "--test=$t"
  done
}

lib_or_bins() {
  if [ -f "$1/src/lib.rs" ] || grep -qE '^\[lib\]' "$1/Cargo.toml"; then
    echo "--lib"
  else
    echo "--bins"
  fi
}

compute_targets() {
  local f c pkg rel name
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    c=$(crate_dir_of "$f") || continue
    pkg=$(pkg_name_of "$c")
    [ -n "$pkg" ] || continue
    if [ "$c" = "." ]; then rel="$f"; else rel="${f#$c/}"; fi
    case "$rel" in
      tests/*/main.rs|tests/*.rs)
        case "$rel" in
          tests/*/*) name=$(echo "$rel" | cut -d/ -f2) ;;
          *) name=$(basename "$rel" .rs) ;;
        esac
        if [ -f "$c/tests/$name.rs" ] || [ -f "$c/tests/$name/main.rs" ]; then
          echo "$pkg --test=$name"
        elif [ "$rel" = "tests/$name.rs" ] || [ "$rel" = "tests/$name/main.rs" ]; then
          :
        else
          all_test_targets "$c" | sed "s|^|$pkg |"
        fi
        ;;
      tests/*)
        all_test_targets "$c" | sed "s|^|$pkg |"
        ;;
      src/*)
        [ -f "$c/$rel" ] && echo "$pkg $(lib_or_bins "$c")"
        ;;
      *)
        echo "$pkg $(lib_or_bins "$c")"
        all_test_targets "$c" | sed "s|^|$pkg |"
        ;;
    esac
  done | sort -u
}
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO_DIR]]
[[COMMON]]
export CARGO_NET_OFFLINE=true
export SQLX_OFFLINE=true
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
export RUST_TEST_THREADS=1
PGV=$(ls /usr/lib/postgresql | sort -V | tail -n1)
pg_ctlcluster "$PGV" main start
for i in $(seq 1 30); do
  pg_isready -h 127.0.0.1 -p 5432 -U postgres && break
  sleep 1
done
pg_isready -h 127.0.0.1 -p 5432 -U postgres || { echo "run: postgres did not start" >&2; exit 1; }

TARGETS=$(compute_targets)
if [ -z "$TARGETS" ]; then
  echo "run: no test.patch targets present at this commit"
  exit 0
fi
overall=0
while read -r pkg sel; do
  [ -n "$pkg" ] || continue
  label="$pkg/${sel#--}"
  label="${label/=/:}"
  echo "===== MSB TARGET BEGIN $label ====="
  cargo test --offline -p "$pkg" "$sel" --no-fail-fast < /dev/null 2>&1
  rc=$?
  echo "===== MSB TARGET END $label rc=$rc ====="
  [ "$rc" -eq 0 ] || overall=$rc
done <<< "$TARGETS"
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
rustc --version
cargo --version

net_retry apt-get update
net_retry apt-get install -y --no-install-recommends postgresql postgresql-contrib libclang-dev clang nodejs
rm -rf /var/lib/apt/lists/*
PGV=$(ls /usr/lib/postgresql | sort -V | tail -n1)
pg_ctlcluster "$PGV" main start
for i in $(seq 1 30); do
  pg_isready -h 127.0.0.1 -p 5432 && break
  sleep 1
done
su postgres -c "psql -v ON_ERROR_STOP=1 -c \"ALTER USER postgres PASSWORD 'postgres'\""
pg_ctlcluster "$PGV" main stop

if [ -f .tree-sitter-cli-version ] && ! command -v tree-sitter >/dev/null 2>&1; then
  net_retry cargo install tree-sitter-cli --version "$(cat .tree-sitter-cli-version)" --locked
fi
export SQLX_OFFLINE=true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
net_retry cargo fetch
TARGETS=$(compute_targets)
echo "prepare: targets:"
echo "$TARGETS"
while read -r pkg sel; do
  [ -n "$pkg" ] || continue
  cargo test --offline -p "$pkg" "$sel" --no-run < /dev/null
done <<< "$TARGETS"
git apply -R --whitespace=nowarn /home/fix.patch
git apply -R --whitespace=nowarn /home/test.patch
bash /home/check_git_changes.sh

"""


def _render(template: str, pr: PullRequest) -> str:
    return (
        template.replace("[[COMMON]]", _COMMON)
        .replace("[[JOBS]]", _JOBS)
        .replace("[[REPO_DIR]]", _REPO_DIR)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class PostgresLanguageServer847To847ImageBase(Image):
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


class PostgresLanguageServer847To847ImageDefault(Image):
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
        return PostgresLanguageServer847To847ImageBase(self.pr, self._config)

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
class PostgresLanguageServer847To847(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PostgresLanguageServer847To847ImageDefault(self.pr, self._config)

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

        target = None
        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            m = _BEGIN_RE.match(line)
            if m:
                target = m.group(1)
                continue
            if _END_RE.match(line):
                target = None
                continue
            if target is None:
                continue
            m = _FAIL_RE.match(line)
            if m:
                failed_tests.add(f"{target}::{m.group(1)}")
                continue
            m = _TEST_RE.match(line)
            if not m:
                continue
            name = f"{target}::{m.group(1)}"
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
