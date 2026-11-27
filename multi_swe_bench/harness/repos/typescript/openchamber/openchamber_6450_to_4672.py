import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.22.3-bookworm-slim"
_NODE_VERSION = "24.22.3"
_INTERVAL = "openchamber_6450_to_4672"
_BASE_TAG = "base-6450_to_4672"

_TEST_RE = re.compile(
    r"^\+\+\+ b/(packages/[^\s/]+)/((?:[^\s/]+/)*[^\s/]+\.(?:test|spec)\.[cm]?[jt]sx?)$",
    re.MULTILINE,
)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _test_dirs(pr: PullRequest) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for workspace, path in _TEST_RE.findall(pr.test_patch or ""):
        found.setdefault(workspace, set()).add(path)
    return {workspace: sorted(paths) for workspace, paths in sorted(found.items())}


def _text_only(patch: str) -> str:
    parts = _DIFF_SPLIT_RE.split(patch or "")
    kept = [
        p
        for p in parts
        if p and not re.search(r"^(?:Binary files .* differ|GIT binary patch)$", p, re.MULTILINE)
    ]
    return "".join(kept)


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "__SHA__^{commit}" 2>/dev/null || git fetch --no-tags origin "__SHA__"
git checkout --detach __SHA__
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"

bun_version="$(node -p "require('./package.json').packageManager" | sed -e 's/^bun@//' -e 's/+.*$//')"
[ -n "$bun_version" ] || { echo "prepare.sh: package.json names no bun version"; exit 1; }
for attempt in 1 2 3; do
    npm install -g "bun@$bun_version" && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: npm install -g bun@$bun_version failed 3 times"; exit 1; }
    sleep 30
done
npm cache clean --force

[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
[ "$(bun --version)" = "$bun_version" ] || { echo "prepare.sh: bun $bun_version is not active"; exit 1; }

for attempt in 1 2 3; do
    bun install --frozen-lockfile && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: bun install failed 3 times"; exit 1; }
    sleep 30
done
__GATES__
echo "gate: Node __NODE_VERSION__, bun from packageManager, frozen lockfile, the PR test runners load"
"""

_GATE_SNIPPET = r"""script="$(cd /home/__REPO__/__DIR__ && node -p "(require('./package.json').scripts || {}).test || ''")"
[[ "$script" == *vitest* || "$script" == *run-isolated-tests* ]] || { echo "prepare.sh: __DIR__ has no vitest or run-isolated-tests script: $script"; exit 1; }
if [[ "$script" == *vitest* ]]; then
    /home/__REPO__/__DIR__/node_modules/.bin/vitest --version || { echo "prepare.sh: vitest does not run in __DIR__"; exit 1; }
else
    (cd /home/__REPO__/__DIR__ && bun test --help > /dev/null) || { echo "prepare.sh: bun test does not run in __DIR__"; exit 1; }
fi
"""

_JS_SH = r"""#!/bin/bash
set -uo pipefail

workspace="$1"
shift
cd "/home/__REPO__/$workspace"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $workspace/$f is not in the tree yet"
    fi
done
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: no test files of $workspace exist yet"
    exit 0
fi

script="$(node -p "(require('./package.json').scripts || {}).test || ''")"
slug="$(echo "$workspace" | tr '/' '_')"
conditions=()
if grep -q -- "--conditions=openchamber-source" /home/__REPO__/scripts/run-isolated-tests.mjs; then
    conditions=(--conditions=openchamber-source)
fi
case "$script" in
    *vitest*)
        out="/tmp/mswb_vitest_${slug}.json"
        rm -f "$out"
        rc=0
        node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
        echo "mswb: vitest exit $rc in $workspace"
        node /home/mswb_digest.js "$out" || exit 1
        if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
            echo "=== MSB: vitest wrote no report in $workspace (status $rc) ==="
            exit 1
        fi
        if [ "$rc" -gt 1 ]; then
            echo "=== MSB: vitest in $workspace exited with status $rc ==="
            exit "$rc"
        fi
        ;;
    *run-isolated-tests*)
        for f in "${files[@]}"; do
            fslug="$(echo "$workspace/$f" | tr '/.' '__')"
            rc=0
            if [[ "$f" =~ \.tsx?$ ]] || grep -qE "(from[[:space:]]+|require\([[:space:]]*)['\"]bun:test['\"]" "$f"; then
                out="/tmp/mswb_bun_${fslug}.xml"
                rm -f "$out"
                timeout -k 10 600 bun test "${conditions[@]}" --reporter=junit --reporter-outfile="$out" "$PWD/$f" < /dev/null || rc=$?
                echo "mswb: bun test exit $rc for $workspace/$f"
                node /home/mswb_junit.js "$out" "$workspace/$f" "$rc" || exit 1
            else
                echo "mswb: $workspace/$f is neither TypeScript nor a bun:test file"
                echo "$workspace/$f::<suite failed to run> FAILED" >> /tmp/mswb_results.txt
            fi
        done
        ;;
    *)
        echo "=== MSB: $workspace has no vitest or run-isolated-tests script: $script ==="
        exit 1
        ;;
esac
exit 0
"""

_DIGEST_JS = r"""const fs = require('fs');
const path = require('path');

const out = process.argv[2];
if (!fs.existsSync(out)) {
  console.log('mswb: no vitest report was written');
  process.exit(0);
}
const raw = fs.readFileSync(out, 'utf8');
if (raw.trim() === '') {
  console.log('mswb: the vitest report is empty');
  process.exit(1);
}
const report = JSON.parse(raw);
const root = '/home/__REPO__';
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const clean = (x) => String(x).replace(/\s+/g, ' ').trim();
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map(clean).join(' > ');
    const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    const key = rel + '::' + title;
    if (!status.has(key) || rank[st] > rank[status.get(key)]) {
      status.set(key, st);
    }
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('mswb: suite error in ' + rel + ': ' + String(file.message || '').split('\n')[0]);
    status.set(rel + '::<suite failed to run>', 'FAILED');
  }
}
const lines = [...status.keys()].sort().map((key) => key + ' ' + status.get(key) + '\n');
fs.appendFileSync('/tmp/mswb_results.txt', lines.join(''));
console.log('mswb: ' + path.basename(out) + ' reported ' + status.size + ' tests');
"""

_JUNIT_JS = r"""const fs = require('fs');

const out = process.argv[2];
const rel = process.argv[3];
const rc = Number(process.argv[4] || 0);
const crash = rel + '::<suite failed to run> FAILED\n';
if (!fs.existsSync(out)) {
  console.log('mswb: bun wrote no junit report for ' + rel);
  if (rc !== 0) {
    fs.appendFileSync('/tmp/mswb_results.txt', crash);
  }
  process.exit(0);
}
const xml = fs.readFileSync(out, 'utf8');
const decode = (s) =>
  s
    .replace(/&#x([0-9a-fA-F]+);/g, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
const clean = (x) => String(x).replace(/\s+/g, ' ').trim();
const attr = (attrs, key) => {
  const m = attrs.match(new RegExp('(?:^|\\s)' + key + '="([^"]*)"'));
  return m ? decode(m[1]) : '';
};
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const suites = [];
let current = null;
const finish = () => {
  if (!current) {
    return;
  }
  const key = rel + '::' + current.title;
  if (!status.has(key) || rank[current.st] > rank[status.get(key)]) {
    status.set(key, current.st);
  }
  current = null;
};
const tag = /<(\/?)(testsuite|testcase|failure|error|skipped)((?:\s+[\w:.-]+="[^"]*")*)\s*(\/?)>/g;
let m;
while ((m = tag.exec(xml)) !== null) {
  const [, closing, name, attrs, selfClosing] = m;
  if (name === 'testsuite') {
    if (closing) {
      suites.pop();
    } else if (!selfClosing) {
      suites.push(clean(attr(attrs, 'name')));
    }
  } else if (name === 'testcase') {
    if (closing) {
      finish();
      continue;
    }
    current = { title: [...suites.slice(1), clean(attr(attrs, 'name'))].join(' > '), st: 'PASSED' };
    if (selfClosing) {
      finish();
    }
  } else if (current && !closing) {
    current.st = name === 'skipped' && current.st !== 'FAILED' ? 'SKIPPED' : 'FAILED';
  }
}
finish();
const lines = [...status.keys()].sort().map((key) => key + ' ' + status.get(key) + '\n');
if ((status.size === 0 && rc !== 0) || rc > 1) {
  lines.push(crash);
}
fs.appendFileSync('/tmp/mswb_results.txt', lines.join(''));
console.log('mswb: bun reported ' + status.size + ' tests for ' + rel);
"""

_REPORT_SH = r"""#!/bin/bash
set -uo pipefail

echo "===== BEGIN TEST RESULTS ====="
if [ -f /tmp/mswb_results.txt ]; then
    LC_ALL=C sort -u /tmp/mswb_results.txt
fi
echo "===== END TEST RESULTS ====="
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class OpenchamberImageBase6450(Image):
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
        return _NODE_IMAGE

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

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class OpenchamberImageDefault6450(Image):
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
        return OpenchamberImageBase6450(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__NODE_VERSION__": _NODE_VERSION,
        }

    def _prepare(self) -> str:
        values = self._values()
        gates = "".join(
            _render(_GATE_SNIPPET, {**values, "__DIR__": workspace})
            for workspace in _test_dirs(self.pr)
        )
        return _render(_PREPARE_SH, {**values, "__GATES__\n": gates})

    def _act(self, name: str, *patches: str) -> File:
        lines = [_RUN_HEADER, "", f"cd /home/{self.pr.repo}", "rm -f /tmp/mswb_results.txt"]
        for patch in patches:
            lines.append(
                f"git apply --whitespace=nowarn {patch} || git apply --3way --whitespace=nowarn {patch} || "
                f'{{ echo "{name}: git apply {patch} failed" >&2; exit 1; }}'
            )
        for workspace, paths in _test_dirs(self.pr).items():
            lines.append(f"bash /home/mswb_js.sh {workspace} {' '.join(paths)}")
        lines.append("bash /home/mswb_report.sh")
        return File(".", name, "\n".join(lines) + "\n")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_js.sh", _render(_JS_SH, values)),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_junit.js", _JUNIT_JS),
            File(".", "mswb_report.sh", _REPORT_SH),
            File(".", "prepare.sh", self._prepare()),
            self._act("run.sh"),
            self._act("test-run.sh", "/home/test.patch"),
            self._act("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}} \\
    CI=true \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    DO_NOT_TRACK=1

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESULTS_RE = re.compile(r"^===== BEGIN TEST RESULTS =====$(.*?)^===== END TEST RESULTS =====$", re.M | re.S)
_LINE_RE = re.compile(
    r"^(?P<name>\S+?\.[cm]?[jt]sx?(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("openchamber", _INTERVAL)
class OPENCHAMBER_6450_TO_4672(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OpenchamberImageDefault6450(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        blocks = _RESULTS_RE.findall(_ANSI_RE.sub("", test_log).replace("\r", ""))
        for line in (blocks[-1] if blocks else "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name"))

        passed, failed, skipped = found["PASSED"], found["FAILED"], found["SKIPPED"]
        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
