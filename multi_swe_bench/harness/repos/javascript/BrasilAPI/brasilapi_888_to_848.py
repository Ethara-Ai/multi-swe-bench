import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "BrasilAPI"
_REPO = "BrasilAPI"
_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"brasilapi_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like brasilapi_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:24.18.1-bookworm"

_LOCKFILE_EXCLUDES = "--exclude=package-lock.json --exclude=yarn.lock --exclude=pnpm-lock.yaml"

_ENV_EXPORTS = (
    "export CI=true\n"
    "export NODE_OPTIONS=--max-old-space-size=4096\n"
    "export NEXT_TELEMETRY_DISABLED=1\n"
    "export HUSKY=0\n"
)

_TEST_CMD = "bash /home/run_tests.sh\n"

_TEST_FILE_RE = re.compile(r"^tests/.+\.test\.js$")

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

_APPLY_FN = """mswe_apply_per_file() {
  echo "mswe_apply: whole-patch apply failed; applying file by file and skipping changes already present at the base commit"
  local d i f
  d=$(mktemp -d)
  i=0
  for p in "$@"; do
    i=$((i + 1))
    awk -v pre="$d/$i" '/^diff --git /{c++; f=sprintf("%s.%05d.diff", pre, c)} f{print > f}' "$p"
  done
  for f in $(ls "$d" | sort -t. -k1,1n -k2,2n); do
    if git apply --check --whitespace=nowarn __EXCLUDES__ "$d/$f" 2>/dev/null; then
      git apply --whitespace=nowarn __EXCLUDES__ "$d/$f"
      echo "mswe_apply: applied $(head -n 1 "$d/$f")"
    elif git apply -R --check --whitespace=nowarn __EXCLUDES__ "$d/$f" 2>/dev/null; then
      echo "mswe_apply: already present at base, skipped $(head -n 1 "$d/$f")"
    else
      echo "mswe_apply: cannot apply $(head -n 1 "$d/$f")"
      git apply --whitespace=nowarn __EXCLUDES__ "$d/$f"
      return 1
    fi
  done
  rm -rf "$d"
}
""".replace("__EXCLUDES__", _LOCKFILE_EXCLUDES)

_EMIT_JS = r"""const fs = require('fs');
const path = require('path');
let data;
try {
  data = JSON.parse(fs.readFileSync('/tmp/mswe-vitest.json', 'utf8'));
} catch (e) {
  console.log('MSWE_NO_JSON');
  process.exit(0);
}
const norm = (s) => String(s).replace(/\s+/g, ' ').trim();
for (const file of data.testResults || []) {
  const rel = path.relative(process.cwd(), file.name).split(path.sep).join('/');
  const asserts = file.assertionResults || [];
  for (const a of asserts) {
    const parts = [rel, ...(a.ancestorTitles || []), a.title].map(norm);
    console.log('MSWE_TEST ' + a.status + ' ' + parts.join(' > '));
  }
  if (file.status === 'failed' && !asserts.some((a) => a.status === 'failed')) {
    console.log('MSWE_FILE_ERROR ' + rel);
  }
}
"""

_RUN_TESTS_TEMPLATE = """#!/bin/bash
set -uo pipefail

cd /home/__REPO__

files=()
for f in __FILES__; do
  if [ ! -f "$f" ]; then
    echo "━━━ Skipping $f (file not found) ━━━"
    continue
  fi
  echo "━━━ Running $f ━━━"
  files+=("$f")
done

if [ "${#files[@]}" -eq 0 ]; then
  echo "MSWE: no test files to run"
  exit 1
fi

rm -f /tmp/mswe-vitest.json /tmp/mswe-next-dev.log

env -u NODE_ENV setsid node node_modules/next/dist/bin/next dev -p 3000 > /tmp/mswe-next-dev.log 2>&1 < /dev/null &
SERVER_PID=$!

cleanup() {
  kill -TERM -- "-$SERVER_PID" 2>/dev/null || kill -TERM "$SERVER_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT

ready=0
for i in $(seq 1 240); do
  if curl -fsS -o /dev/null --max-time 120 http://localhost:3000/api/status/v1 2>/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
if [ "$ready" -eq 1 ]; then
  echo "MSWE_SERVER ready"
else
  echo "MSWE_SERVER not ready"
  tail -n 80 /tmp/mswe-next-dev.log
fi

timeout --kill-after=30 1800 node_modules/.bin/vitest run --reporter=verbose --reporter=json --outputFile.json=/tmp/mswe-vitest.json "${files[@]}" 2>&1
status=$?

node - <<'MSWE_EMIT_EOF'
__EMIT_JS__MSWE_EMIT_EOF

exit $status
"""


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.MULTILINE)
    return sorted({f for f in files if _TEST_FILE_RE.match(f)})


_CALL_RE = re.compile(
    r"^(\s*)(describe|test|it)"
    r"(?:\.(?:only|skip|todo|concurrent|sequential|fails|skipIf\([^()]*\)|runIf\([^()]*\)))*"
    r"\(\s*(['\"`])(.+?)\3\s*,"
)


def _target_tests(test_patch: str) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {}
    current = None
    stack: list[tuple[int, str]] = []
    for line in test_patch.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git a/\S+ b/(\S+)$", line)
            current = m.group(1) if m and _TEST_FILE_RE.match(m.group(1)) else None
            stack = []
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            stack = []
            continue
        if line.startswith(("+++", "---", "-", "\\")):
            continue
        added = line.startswith("+")
        body = line[1:]
        m = _CALL_RE.match(body)
        if not m:
            continue
        indent = len(m.group(1).replace("\t", "  "))
        title = m.group(4)
        if "${" in title:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if m.group(2) == "describe":
            stack.append((indent, title))
            continue
        if not added:
            continue
        expected = [i * 2 for i in range(len(stack))]
        if [s[0] for s in stack] != expected or indent != len(stack) * 2:
            continue
        name = " > ".join([current] + [s[1] for s in stack] + [title])
        targets.setdefault(current, set()).add(re.sub(r"\s+", " ", name).strip())
    return targets


class BrasilApiImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo
        enh = DockerfileEnhancer

        label_block = (
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        return f"""{enh.SYNTAX_DIRECTIVE}

FROM {image_name}

{enh._TARGETARCH_ARG}
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{enh._PROXY_ARGS}

{enh._ENV_BLOCK}

{label_block}

{enh._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class BrasilApiImageDefault(Image):
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
        return BrasilApiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""#!/bin/bash
set -e

{_ENV_EXPORTS}
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

node --version
npm --version
timeout --kill-after=30 1200 npm ci --no-audit --no-fund || true
git checkout -- package-lock.json

cat > /home/run_tests.sh <<'MSWE_RUN_TESTS_EOF'
{self._run_tests_sh()}MSWE_RUN_TESTS_EOF
chmod +x /home/run_tests.sh

node -e "require('./package.json'); require('next/package.json'); require('vitest/package.json'); require('rollup'); require('async-retry'); require('axios'); console.log('DEPS_OK')"
test -x node_modules/.bin/vitest
test -f node_modules/next/dist/bin/next
ls -d node_modules/@next/swc-linux-*-gnu
command -v curl
command -v setsid
test -s /home/run_tests.sh
"""

    def _run_tests_sh(self) -> str:
        return (
            _RUN_TESTS_TEMPLATE.replace("__REPO__", self.pr.repo)
            .replace("__FILES__", " ".join(_test_files(self.pr)))
            .replace("__EMIT_JS__", _EMIT_JS)
        )

    def _run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_TEST_CMD}"""

    def _test_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_APPLY_FN}
set +e
git apply --whitespace=nowarn {_LOCKFILE_EXCLUDES} /home/test.patch
apply_rc=$?
set -e
if [ "$apply_rc" -ne 0 ]; then
  mswe_apply_per_file /home/test.patch
fi
{_TEST_CMD}"""

    def _fix_run_sh(self) -> str:
        return f"""#!/bin/bash
set -eo pipefail

{_ENV_EXPORTS}
cd /home/{self.pr.repo}
{_APPLY_FN}
set +e
git apply --whitespace=nowarn {_LOCKFILE_EXCLUDES} /home/test.patch /home/fix.patch
apply_rc=$?
set -e
if [ "$apply_rc" -ne 0 ]; then
  mswe_apply_per_file /home/test.patch /home/fix.patch
fi
{_TEST_CMD}"""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._prepare_sh()),
            File(".", "run.sh", self._run_sh()),
            File(".", "test-run.sh", self._test_run_sh()),
            File(".", "fix-run.sh", self._fix_run_sh()),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_MSWE_TEST_RE = re.compile(r"^MSWE_TEST (passed|failed|skipped|pending|todo|disabled) (.+?)\s*$")
_MSWE_FILE_ERROR_RE = re.compile(r"^MSWE_FILE_ERROR (\S+)\s*$")
_VERBOSE_RE = re.compile(
    r"^\s*(✓|✔|×|✗|✘|❌|↓)️?\s+(tests/\S+\.test\.js\s+>\s+.+?)"
    r"(?:\s+\[skipped\])?(?:\s+\d+(?:\.\d+)?\s*m?s)?\s*$"
)
_STATUS = {
    "passed": "pass",
    "failed": "fail",
    "skipped": "skip",
    "pending": "skip",
    "todo": "skip",
    "disabled": "skip",
    "✓": "pass",
    "✔": "pass",
    "×": "fail",
    "✗": "fail",
    "✘": "fail",
    "❌": "fail",
    "↓": "skip",
}


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class BRASILAPI_888_TO_848(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO}#{pr.number} is outside {_ERA_FILE} (#{_ERA_LO}-#{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return BrasilApiImageDefault(self.pr, self._config)

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
        errored_files: set[str] = set()

        clean = _ANSI_RE.sub("", test_log).replace("\r", "")
        lines = [raw.rstrip() for raw in clean.split("\n")]
        buckets = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}

        structured = False
        for line in lines:
            m = _MSWE_TEST_RE.match(line)
            if m:
                structured = True
                name = re.sub(r"\s+", " ", m.group(2)).strip()
                buckets[_STATUS[m.group(1)]].add(name)
                continue
            m = _MSWE_FILE_ERROR_RE.match(line)
            if m:
                structured = True
                errored_files.add(m.group(1))

        if not structured:
            for line in lines:
                m = _VERBOSE_RE.match(line)
                if m:
                    name = re.sub(r"\s+", " ", m.group(2)).strip()
                    buckets[_STATUS[m.group(1)]].add(name)

        if errored_files:
            targets = _target_tests(self.pr.test_patch or "")
            for f in errored_files:
                for name in targets.get(f, set()):
                    if name not in passed_tests and name not in skipped_tests:
                        failed_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
