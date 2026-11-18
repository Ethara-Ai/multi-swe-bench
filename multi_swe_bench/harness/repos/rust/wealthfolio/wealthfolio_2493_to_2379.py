import html
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "wealthfolio"
_INTERVAL = "wealthfolio_2493_to_2379"
_BASE_IMAGE = "rust:1.98-bookworm"
_APT_PACKAGES = "pkg-config libssl-dev xz-utils cmake"

_BEGIN = "===== MSB CARGO TEST BEGIN ====="
_END = "===== MSB CARGO TEST END ====="
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RUNNING_RE = re.compile(r"^\s*Running (?:unittests )?(\S+) \((?:.*/)?deps/([A-Za-z0-9_]+)-[0-9a-f]+(?:\.exe)?\)")
_TEST_RE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
_JUNIT_BEGIN = "===== MSB JUNIT BEGIN ====="
_JUNIT_END = "===== MSB JUNIT END ====="
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

_ENV = r"""export CARGO_BUILD_JOBS=4
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export RUST_BACKTRACE=0
export PATH="/usr/local/cargo/bin:/usr/local/node/bin:$PATH"
"""

_TARGETS_FN = r"""msb_targets() {
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      *.rs) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ ! -f "$d/Cargo.toml" ]; do
      d=$(dirname "$d")
    done
    [ -f "$d/Cargo.toml" ] || continue
    rel="${f#$d/}"
    case "$rel" in
      tests/*/*) t="${rel#tests/}"; echo "$d --test ${t%%/*}" ;;
      tests/*.rs) t="${rel#tests/}"; echo "$d --test ${t%.rs}" ;;
      src/*)
        if [ -f "$d/src/lib.rs" ]; then echo "$d --lib"; fi
        if [ -f "$d/src/main.rs" ] || [ -d "$d/src/bin" ]; then echo "$d --bins"; fi
        ;;
    esac
  done | sort -u
}

msb_js_targets() {
  for f in $(grep -oE '^diff --git a/[^ ]+' /home/test.patch | cut -c14- | sort -u); do
    case "$f" in
      *.test.ts|*.test.tsx|*.test.js|*.test.jsx|*.test.mts|*.spec.ts|*.spec.tsx|*.spec.js|*.spec.jsx) ;;
      *) continue ;;
    esac
    [ -f "$f" ] || continue
    d=$(dirname "$f")
    while [ "$d" != "." ] && [ ! -f "$d/package.json" ]; do
      d=$(dirname "$d")
    done
    [ -f "$d/package.json" ] || continue
    echo "$d ${f#$d/}"
  done | sort -u
}
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail
[[ENV]]
cd /home/[[REPO]]
[[TARGETS_FN]]
declare -A ARGS
for line in $(msb_targets | tr ' ' '|'); do
  d="${line%%|*}"
  a="${line#*|}"
  ARGS[$d]="${ARGS[$d]} ${a//|/ }"
done
rc=0
echo "[[BEGIN]]"
for d in "${!ARGS[@]}"; do
  echo "MSB_CARGO_TARGET $d:${ARGS[$d]}"
  cargo test --offline --no-fail-fast --manifest-path "$d/Cargo.toml" ${ARGS[$d]} 2>&1 || rc=1
done
echo "[[END]]"
declare -A JS
for line in $(msb_js_targets | tr ' ' '|'); do
  d="${line%%|*}"
  JS[$d]="${JS[$d]} ${line#*|}"
done
n=0
for d in "${!JS[@]}"; do
  n=$((n + 1))
  out="/tmp/msb-vitest-$n.xml"
  rm -f "$out"
  echo "MSB_VITEST_TARGET $d:${JS[$d]}"
  (cd "$d" && pnpm exec vitest run --no-file-parallelism --testTimeout=60000 --reporter=default --reporter=junit --outputFile.junit="$out" ${JS[$d]}) 2>&1 || rc=1
  echo "[[JUNIT_BEGIN]]"
  if [ -f "$out" ]; then
    cat "$out"
  fi
  echo "[[JUNIT_END]]"
done
exit $rc
"""

_PREPARE_SH = r"""#!/bin/bash
set -eo pipefail
[[ENV]]
retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    if [ "$n" -ge 5 ]; then
      return 1
    fi
    sleep $((n * 10))
  done
}

cd /home/[[REPO]]
git reset --hard
bash /home/check_git_changes.sh
if ! git cat-file -e "[[SHA]]^{commit}" 2>/dev/null; then
  retry git fetch --no-tags [[REPO_URL]] "[[SHA]]"
fi
git checkout "[[SHA]]"
bash /home/check_git_changes.sh

retry apt-get update
apt-get install -y --no-install-recommends [[APT]]
rm -rf /var/lib/apt/lists/*

if [ -f rust-toolchain.toml ] || [ -f rust-toolchain ]; then
  retry rustup toolchain install
else
  REQ=$(find . -name Cargo.toml -not -path './target/*' -not -path '*/node_modules/*' -exec grep -hoE '^rust-version *= *"[0-9.]+"' {} + | grep -oE '[0-9]+(\.[0-9]+)+' | sort -V | tail -1)
  CUR=$(rustc --version | awk '{print $2}')
  if [ -n "$REQ" ] && [ "$(printf '%s\n%s\n' "$REQ" "$CUR" | sort -V | head -1)" != "$REQ" ]; then
    retry rustup toolchain install "$REQ" --profile minimal
    rustup default "$REQ"
  fi
fi
rustc --version
cargo --version

NODE_SPEC=""
for vf in .node-version .nvmrc; do
  if [ -z "$NODE_SPEC" ] && [ -f "$vf" ]; then
    NODE_SPEC=$(head -1 "$vf" | tr -d ' \r\n' | sed 's/^v//')
  fi
done
if [ -z "$NODE_SPEC" ] && [ -f package.json ]; then
  NODE_SPEC=$(sed -n '/"engines"/,/}/p' package.json | grep -oE '"node" *: *"[^"]*"' | sed -E 's/"node" *: *"//' | grep -oE '[0-9]+' | head -1)
fi
if [ -n "$NODE_SPEC" ]; then
  case "$(dpkg --print-architecture)" in
    amd64) NARCH=x64 ;;
    arm64) NARCH=arm64 ;;
    *) echo "unsupported arch" >&2; exit 1 ;;
  esac
  if echo "$NODE_SPEC" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    NODE_BASE="https://nodejs.org/dist/v${NODE_SPEC}"
  else
    NODE_BASE="https://nodejs.org/dist/latest-v${NODE_SPEC%%.*}.x"
  fi
  retry curl -fsSL -o /tmp/node-shasums.txt "$NODE_BASE/SHASUMS256.txt"
  NODE_TAR=$(grep -oE "node-v[0-9.]+-linux-${NARCH}\.tar\.xz" /tmp/node-shasums.txt | head -1)
  retry curl -fsSL -o "/tmp/$NODE_TAR" "$NODE_BASE/$NODE_TAR"
  (cd /tmp && grep " $NODE_TAR\$" node-shasums.txt | sha256sum -c -)
  rm -rf /usr/local/node
  mkdir -p /usr/local/node
  tar -xJf "/tmp/$NODE_TAR" -C /usr/local/node --strip-components=1
  rm -f "/tmp/$NODE_TAR" /tmp/node-shasums.txt
  node --version
  PNPM_SPEC=$(grep -oE '"packageManager" *: *"pnpm@[^"+]+' package.json | sed -E 's/.*pnpm@//')
  if [ -n "$PNPM_SPEC" ]; then
    retry npm install -g "pnpm@$PNPM_SPEC"
    pnpm --version
    export CI=true
    retry pnpm install --frozen-lockfile
  fi
fi

[[TARGETS_FN]]
build_targets() {
  declare -A ARGS
  for line in $(msb_targets | tr ' ' '|'); do
    d="${line%%|*}"
    a="${line#*|}"
    ARGS[$d]="${ARGS[$d]} ${a//|/ }"
  done
  for d in "${!ARGS[@]}"; do
    cargo test --offline --no-run --manifest-path "$d/Cargo.toml" ${ARGS[$d]}
  done
}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
for d in $(msb_targets | awk '{print $1}' | sort -u); do
  retry cargo fetch --manifest-path "$d/Cargo.toml"
done
build_targets
git reset --hard
git clean -fd
bash /home/check_git_changes.sh
build_targets
bash /home/check_git_changes.sh

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
        template.replace("[[ENV]]", _ENV)
        .replace("[[TARGETS_FN]]", _TARGETS_FN)
        .replace("[[REPO]]", pr.repo)
        .replace("[[APT]]", _APT_PACKAGES)
        .replace("[[BEGIN]]", _BEGIN)
        .replace("[[END]]", _END)
        .replace("[[JUNIT_BEGIN]]", _JUNIT_BEGIN)
        .replace("[[JUNIT_END]]", _JUNIT_END)
        .replace("[[SHA]]", pr.base.sha)
        .replace("[[REPO_URL]]", f"https://github.com/{pr.org}/{pr.repo}.git")
    )


def _base_tag(pr: PullRequest) -> str:
    return "base"


class Wealthfolio2493To2379ImageBase(Image):
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
        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {_BASE_IMAGE}

{self.global_env}

WORKDIR /home/

{code}

{self.clear_env}

"""


class Wealthfolio2493To2379ImageDefault(Image):
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
        return Wealthfolio2493To2379ImageBase(self.pr, self._config)

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
            File(".", "prepare.sh", _render(_PREPARE_SH, self.pr)),
            File(".", "run.sh", _render(_RUN_SH, self.pr)),
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
class Wealthfolio2493To2379(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Wealthfolio2493To2379ImageDefault(self.pr, self._config)

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

        start = test_log.find(_BEGIN)
        end = test_log.rfind(_END)
        if start == -1:
            section = ""
        elif end == -1 or end < start:
            section = test_log[start + len(_BEGIN) :]
        else:
            section = test_log[start + len(_BEGIN) : end]

        binary = ""
        for raw in section.splitlines():
            line = _ANSI_RE.sub("", raw).rstrip()
            m = _RUNNING_RE.match(line)
            if m:
                binary = m.group(2)
                continue
            m = _TEST_RE.match(line)
            if not m:
                continue
            name = f"{binary}::{m.group(1)}" if binary else m.group(1)
            status = m.group(2)
            if status == "ok":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        pos = 0
        while True:
            start = test_log.find(_JUNIT_BEGIN, pos)
            if start == -1:
                break
            end = test_log.find(_JUNIT_END, start)
            if end == -1:
                end = len(test_log)
            block = test_log[start + len(_JUNIT_BEGIN) : end]
            pos = end
            for m in _CASE_RE.finditer(block):
                attrs = dict(_ATTR_RE.findall(m.group(1)))
                name = html.unescape(
                    f"{attrs.get('classname', '')}::{attrs.get('name', '')}"
                )
                body = m.group(3) or ""
                if "<failure" in body or "<error" in body:
                    failed_tests.add(name)
                elif "<skipped" in body:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

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
