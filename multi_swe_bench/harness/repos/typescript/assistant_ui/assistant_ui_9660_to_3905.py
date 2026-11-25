import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.22.0-bookworm-slim"
_NODE_VERSION = "24.22.0"
_INTERVAL = "assistant_ui_9660_to_3905"
_BASE_TAG = "base-9660_to_3905"

_TEST_RE = re.compile(
    r"^\+\+\+ b/((?:packages|apps|examples|templates)/[^\s/]+|evals|api-surface)/"
    r"((?:[^\s/]+/)*[^\s/]+\.(?:test|spec)\.[cm]?[jt]sx?)$",
    re.MULTILINE,
)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _test_dirs(pr: PullRequest) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for workspace, path in _TEST_RE.findall(pr.test_patch or ""):
        found.setdefault(workspace, set()).add(path)
    return {workspace: sorted(paths) for workspace, paths in sorted(found.items())}


def _build_filter(workspace: str) -> str:
    if workspace.startswith("packages/"):
        return f"'{{./{workspace}}}...'"
    return f"'{{./{workspace}}}^...'"


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

pnpm_version="$(node -p "require('./package.json').packageManager" | sed -e 's/^pnpm@//' -e 's/+.*$//')"
[ -n "$pnpm_version" ] || { echo "prepare.sh: package.json names no pnpm version"; exit 1; }
for attempt in 1 2 3; do
    npm install -g "pnpm@$pnpm_version" && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: npm install -g pnpm@$pnpm_version failed 3 times"; exit 1; }
    sleep 30
done
npm cache clean --force

[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
[ "$(pnpm -v)" = "$pnpm_version" ] || { echo "prepare.sh: pnpm $pnpm_version is not active"; exit 1; }

for attempt in 1 2 3; do
    pnpm install --frozen-lockfile && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
    sleep 30
done
pnpm exec turbo --version
pnpm exec turbo run build __BUILD_FILTERS__ --output-logs=errors-only || { echo "prepare.sh: turbo build failed"; exit 1; }
__GATES__
echo "gate: Node __NODE_VERSION__, pnpm from packageManager, frozen lockfile, tested workspaces built, the PR test runners load"
"""

_GATE_SNIPPET = r"""test -x /home/__REPO__/__DIR__/node_modules/.bin/vitest || { echo "prepare.sh: vitest is not installed in __DIR__"; exit 1; }
pnpm --dir /home/__REPO__/__DIR__ exec vitest --version
"""

_BUILD_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__
rc=0
pnpm exec turbo run build "$@" --output-logs=errors-only < /dev/null || rc=$?
if [ "$rc" -ne 0 ]; then
    echo "=== MSB: turbo build exited with status $rc ==="
    exit 1
fi
echo "mswb: turbo build ok"
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
rc=0
case "$script" in
    *vitest*)
        out="/tmp/mswb_vitest_${slug}.json"
        rm -f "$out"
        node_modules/.bin/vitest run --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
        echo "mswb: vitest exit $rc in $workspace"
        node /home/mswb_digest.js "$out" || exit 1
        if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
            echo "=== MSB: vitest wrote no report in $workspace (status $rc) ==="
            exit 1
        fi
        ;;
    *"node --test"*)
        out="/tmp/mswb_node_${slug}.txt"
        rm -f "$out"
        node --test --test-concurrency=1 --test-reporter=/home/mswb_node_reporter.mjs --test-reporter-destination="$out" "${files[@]}" < /dev/null || rc=$?
        echo "mswb: node --test exit $rc in $workspace"
        if [ -f "$out" ]; then
            cat "$out" >> /tmp/mswb_results.txt
            echo "mswb: node --test reported $(wc -l < "$out") tests in $workspace"
        elif [ "$rc" -ne 0 ]; then
            echo "=== MSB: node --test wrote no report in $workspace (status $rc) ==="
            exit 1
        fi
        ;;
    *)
        echo "=== MSB: $workspace has no vitest or node --test script: $script ==="
        exit 1
        ;;
esac
if [ "$rc" -gt 1 ]; then
    echo "=== MSB: the test runner in $workspace exited with status $rc ==="
    exit "$rc"
fi
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
  }
}
const lines = [...status.keys()].sort().map((key) => key + ' ' + status.get(key) + '\n');
fs.appendFileSync('/tmp/mswb_results.txt', lines.join(''));
console.log('mswb: ' + path.basename(out) + ' reported ' + status.size + ' tests');
"""

_NODE_REPORTER_MJS = r"""import path from 'node:path';

const root = '/home/__REPO__';
const clean = (x) => String(x).replace(/\s+/g, ' ').trim();

export default async function* reporter(source) {
  const stacks = new Map();
  for await (const event of source) {
    const d = event.data || {};
    if (!d.file) {
      continue;
    }
    const file = path.resolve(d.file);
    if (path.resolve(String(d.name)) === file) {
      continue;
    }
    if (!stacks.has(file)) {
      stacks.set(file, []);
    }
    const stack = stacks.get(file);
    if (event.type === 'test:start') {
      stack.length = d.nesting;
      stack.push(clean(d.name));
      continue;
    }
    if (event.type !== 'test:pass' && event.type !== 'test:fail') {
      continue;
    }
    if (d.details && d.details.type === 'suite') {
      continue;
    }
    const names = stack.slice(0, d.nesting);
    names.push(clean(d.name));
    const st = d.skip || d.todo ? 'SKIPPED' : event.type === 'test:pass' ? 'PASSED' : 'FAILED';
    yield path.relative(root, file) + '::' + names.join(' > ') + ' ' + st + '\n';
  }
}
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


class AssistantUiImageBase9660(Image):
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


class AssistantUiImageDefault9660(Image):
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
        return AssistantUiImageBase9660(self.pr, self.config)

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
        dirs = _test_dirs(self.pr)
        build = " ".join(f"--filter={_build_filter(workspace)}" for workspace in dirs)
        gates = "".join(
            _render(_GATE_SNIPPET, {**values, "__DIR__": workspace})
            for workspace in dirs
            if workspace != "evals"
        )
        return _render(
            _PREPARE_SH,
            {
                **values,
                "__BUILD_FILTERS__": build,
                "__GATES__\n": gates,
            },
        )

    def _act(self, name: str, *patches: str) -> File:
        lines = [_RUN_HEADER, "", f"cd /home/{self.pr.repo}", "rm -f /tmp/mswb_results.txt"]
        for patch in patches:
            lines.append(
                f"git apply --whitespace=nowarn {patch} || git apply --3way --whitespace=nowarn {patch} || "
                f'{{ echo "{name}: git apply {patch} failed" >&2; exit 1; }}'
            )
        dirs = _test_dirs(self.pr)
        if dirs:
            lines.append(
                "bash /home/mswb_build.sh " + " ".join(f"--filter={_build_filter(w)}" for w in dirs)
            )
        for workspace, paths in dirs.items():
            lines.append(f"bash /home/mswb_js.sh {workspace} {' '.join(paths)}")
        lines.append("bash /home/mswb_report.sh")
        return File(".", name, "\n".join(lines) + "\n")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_build.sh", _render(_BUILD_SH, values)),
            File(".", "mswb_js.sh", _render(_JS_SH, values)),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_node_reporter.mjs", _render(_NODE_REPORTER_MJS, values)),
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
    pnpm_config_verify_deps_before_run=false \\
    TURBO_TELEMETRY_DISABLED=1 \\
    TURBO_NO_UPDATE_NOTIFIER=1 \\
    DO_NOT_TRACK=1 \\
    NEXT_TELEMETRY_DISABLED=1

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


@Instance.register("assistant-ui", _INTERVAL)
class ASSISTANT_UI_9660_TO_3905(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return AssistantUiImageDefault9660(self.pr, self._config)

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
