import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.97.0-bookworm"
_NODE_IMAGE = "node:24-bookworm"
_PNPM_VERSION = "11.1.1"
_UV_VERSION = "0.9.5"
_VENV = "/opt/lancedb-venv"
_TOOLS = "/opt/lancedb-tools"

_PY_RE = r"^python/python/tests/(.*/)?test_[^/]*\.py$"
_RS_RE = r"^rust/lancedb/tests/[^/]+\.rs$"
_JS_RE = r"^nodejs/__test__/.+\.test\.ts$"

_FEATURES = "--no-default-features --features remote,lancedb/metrics-otel"
_PY_BUILD = f"maturin develop --skip-install --locked --profile ci {_FEATURES}"
_JS_BUILD = f"pnpm build:debug --profile ci {_FEATURES}"
_RS_FLAGS = "-p lancedb --profile ci --locked --features remote,metrics-otel"
_PYTEST = 'python -m pytest -rA -p no:cacheprovider -m "not slow and not s3_test"'

_ENV = f"""export CI=true
export NO_COLOR=1
export FORCE_COLOR=0
export CARGO_TERM_COLOR=never
export CARGO_INCREMENTAL=0
export CARGO_PROFILE_CI_DEBUG=0
export CARGO_PROFILE_CI_STRIP=debuginfo
export CARGO_NET_RETRY=10
export CARGO_BUILD_JOBS="${{CARGO_BUILD_JOBS:-4}}"
export RUST_BACKTRACE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_PYTHON_DOWNLOADS=never
export UV_LINK_MODE=copy
export UV_PROJECT_ENVIRONMENT={_VENV}
export VIRTUAL_ENV={_VENV}
export NODE_OPTIONS=--experimental-vm-modules
export PATH={_VENV}/bin:{_TOOLS}/bin:$PATH"""

_RUN_ENV = """export CARGO_NET_OFFLINE=true
export UV_OFFLINE=1
export PIP_NO_INDEX=1"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_SELECT = """#!/bin/bash
mapfile -t MSWE_CHANGED < <(sed -n -E 's#^diff --git a/[^ ]+ b/([^ ]+)$#\\1#p' /home/test.patch | sort -u)
mswe_pick() {
  local re="$1" mode="$2" f
  for f in "${MSWE_CHANGED[@]}"; do
    if [[ "$f" =~ $re ]]; then
      if [ "$mode" != "existing" ] || [ -f "$f" ]; then
        echo "$f"
      fi
    fi
  done
}
"""

_REPORT = r"""const fs = require("fs");
const [file, rel] = process.argv.slice(2);
let data = null;
try {
  data = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (e) {
  process.exit(0);
}
const clean = (s) => String(s).replace(/\s+/g, " ").trim();
const seen = new Map();
for (const result of data.testResults || []) {
  for (const a of result.assertionResults || []) {
    const parts = [...(a.ancestorTitles || []), a.title].map(clean).filter(Boolean);
    let name = `${rel}::${parts.join(" > ")}`;
    const n = (seen.get(name) || 0) + 1;
    seen.set(name, n);
    if (n > 1) name = `${name} [${n}]`;
    const status = a.status === "passed" ? "PASSED" : a.status === "failed" ? "FAILED" : "SKIPPED";
    console.log(`MSWE_CASE ${status} ${name}`);
  }
}
"""


def _run_tests(repo: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
{_ENV}
{_RUN_ENV}
cd /home/{repo}
source /home/mswe-select.sh
mapfile -t PY < <(mswe_pick '{_PY_RE}' existing)
mapfile -t RS < <(mswe_pick '{_RS_RE}' existing)
mapfile -t JS < <(mswe_pick '{_JS_RE}' existing)
status=0
if [ "${{#PY[@]}}" -gt 0 ]; then
  if (cd python && {_PY_BUILD}); then
    for f in "${{PY[@]}}"; do
      echo "MSWE_CTX $f"
      if (cd python && {_PYTEST} "${{f#python/}}"); then
        echo "MSWE_FILE PASSED $f"
      else
        echo "MSWE_FILE FAILED $f"
        status=1
      fi
    done
  else
    for f in "${{PY[@]}}"; do echo "MSWE_FILE FAILED $f"; done
    status=1
  fi
fi
for f in "${{RS[@]}}"; do
  echo "MSWE_CTX $f"
  if cargo test {_RS_FLAGS} --test "$(basename "$f" .rs)"; then
    echo "MSWE_FILE PASSED $f"
  else
    echo "MSWE_FILE FAILED $f"
    status=1
  fi
done
if [ "${{#JS[@]}}" -gt 0 ]; then
  if (cd nodejs && {_JS_BUILD}); then
    (cd nodejs && pnpm tsc) || true
    for f in "${{JS[@]}}"; do
      echo "MSWE_CTX $f"
      rm -f /tmp/mswe-jest.json
      if (cd nodejs && node_modules/.bin/jest --ci --json --outputFile=/tmp/mswe-jest.json --runTestsByPath "${{f#nodejs/}}"); then
        echo "MSWE_FILE PASSED $f"
      else
        echo "MSWE_FILE FAILED $f"
        status=1
      fi
      node /home/mswe-report.cjs /tmp/mswe-jest.json "$f" || status=1
    done
  else
    for f in "${{JS[@]}}"; do echo "MSWE_FILE FAILED $f"; done
    status=1
  fi
fi
exit $status
"""


def _stage(repo: str, patches: str) -> str:
    apply = f"git apply --whitespace=nowarn {patches} || exit 1\n" if patches else ""
    return f"#!/bin/bash\nset -eo pipefail\ncd /home/{repo}\n{apply}bash /home/run-tests.sh\n"


def _prepare(repo: str, sha: str) -> str:
    return f"""#!/bin/bash
set -e
{_ENV}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

retry() {{
  local attempt
  for attempt in 1 2 3 4 5; do
    if "$@"; then
      return 0
    fi
    sleep 20
  done
  return 1
}}

source /home/mswe-select.sh
PY="$(mswe_pick '{_PY_RE}')"
RS="$(mswe_pick '{_RS_RE}')"
JS="$(mswe_pick '{_JS_RE}')"

retry cargo fetch --locked || true
cargo metadata --locked --offline --format-version 1 > /dev/null

if [ -n "$PY" ]; then
  cd /home/{repo}/python
  maturin_version="$(python3 -c 'import tomllib; d = tomllib.load(open("uv.lock", "rb")); print(next(p["version"] for p in d["package"] if p["name"] == "maturin"))')"
  python3 -m venv {_TOOLS}
  retry {_TOOLS}/bin/pip install "uv=={_UV_VERSION}" "maturin==$maturin_version" || true
  retry uv sync --frozen --no-dev --no-install-project --extra tests --python /usr/bin/python3 || true
  uv cache clean || true
  maturin develop --uv --locked --profile ci {_FEATURES} || true
  python -c "import lancedb, lancedb._lancedb, pytest; print(lancedb.__file__)"
  CARGO_NET_OFFLINE=true {_PY_BUILD} || true
fi

if [ -n "$RS" ]; then
  cd /home/{repo}
  targets=()
  for f in $(mswe_pick '{_RS_RE}' existing); do
    targets+=(--test "$(basename "$f" .rs)")
  done
  if [ "${{#targets[@]}}" -eq 0 ]; then
    targets=(--lib)
  fi
  cargo test {_RS_FLAGS} "${{targets[@]}}" --no-run || true
fi

if [ -n "$JS" ]; then
  cd /home/{repo}/nodejs
  retry pnpm install --frozen-lockfile --ignore-scripts || true
  {_JS_BUILD} || true
  pnpm tsc || true
fi

cd /home/{repo}
if [ -n "$JS" ]; then nodejs/node_modules/.bin/jest --version; fi
if [ -n "$RS" ]; then cargo metadata --offline --format-version 1 > /dev/null; fi
test "$(git rev-parse HEAD)" = "{sha}"
echo DEPS_OK
"""


class LancedbRangeImageBase(Image):
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
        return f"range-base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"range-base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return ["pkg-config", "libssl-dev", "protobuf-compiler", "libprotobuf-dev", "python3-dev", "python3-venv"]

    def extra_setup(self) -> str:
        return (
            "ENV CARGO_HTTP_CAINFO=${CA_CERT_PATH} \\\n"
            "    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \\\n"
            "    CARGO_TERM_COLOR=never \\\n"
            "    CI=true\n"
            f"COPY --from={_NODE_IMAGE} /usr/local/bin/node /usr/local/bin/node\n"
            f"COPY --from={_NODE_IMAGE} /usr/local/lib/node_modules /usr/local/lib/node_modules\n"
            "RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \\\n"
            "    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \\\n"
            f"    && npm install -g pnpm@{_PNPM_VERSION} \\\n"
            "    && git config --global --add safe.directory '*' \\\n"
            "    && rustc --version && protoc --version && node --version && pnpm --version"
        )


class LancedbRangeImageDefault(Image):
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
        return LancedbRangeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "mswe-select.sh", _SELECT),
            File(".", "mswe-report.cjs", _REPORT),
            File(".", "prepare.sh", _prepare(repo, self.pr.base.sha)),
            File(".", "run-tests.sh", _run_tests(repo)),
            File(".", "run.sh", _stage(repo, "")),
            File(".", "test-run.sh", _stage(repo, "/home/test.patch")),
            File(".", "fix-run.sh", _stage(repo, "/home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {base.image_full_name()}
{self.global_env}
{copy_commands}
RUN bash /home/prepare.sh
{self.clear_env}
"""


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_CTX = re.compile(r"^MSWE_CTX (\S+)$")
_CASE = re.compile(r"^MSWE_(?:CASE|FILE) (PASSED|FAILED|SKIPPED) (.+)$")
_CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)\b")
_PYTEST_LINE = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL) (\S+\.py::.+?)(?: - .*)?$")


@Instance.register("lancedb", "lancedb_4647_to_4589")
class LancedbRange(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return LancedbRangeImageDefault(self.pr, self._config)

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
        buckets = {k: passed_tests for k in ("PASSED", "ok", "XPASS")}
        buckets.update({k: failed_tests for k in ("FAILED", "ERROR")})
        buckets.update({k: skipped_tests for k in ("SKIPPED", "ignored", "XFAIL")})
        ctx = ""
        for raw in _ANSI.sub("", test_log).splitlines():
            line = raw.strip()
            found = _CTX.match(line)
            if found:
                ctx = found.group(1)
                continue
            found = _CASE.match(line)
            if found:
                buckets[found.group(1)].add(found.group(2).strip())
                continue
            found = _CARGO.match(line)
            if found:
                buckets[found.group(2)].add(f"{ctx}::{found.group(1)}")
                continue
            found = _PYTEST_LINE.match(line)
            if found:
                buckets[found.group(1)].add(found.group(2).strip())

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
