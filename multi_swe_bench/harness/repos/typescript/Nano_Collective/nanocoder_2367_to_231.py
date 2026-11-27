import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.23.5-bookworm-slim"
_NODE_VERSION = "22.23.5"
_PNPM_FALLBACK = "10.28.1"
_INTERVAL = "nanocoder_2367_to_231"
_BASE_TAG = "base-2367_to_231"

_TEST_RE = re.compile(
    r"^\+\+\+ b/((?:source|scripts|benchmarks|plugins)/(?:[^\s/]+/)*[^\s/]+\.spec\.tsx?)$",
    re.MULTILINE,
)
_DIFF_SPLIT_RE = re.compile(r"(?=^diff --git )", re.MULTILINE)


def _test_files(pr: PullRequest) -> list[str]:
    return sorted(set(_TEST_RE.findall(pr.test_patch or "")))


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

pnpm_version="$(node -p "require('./package.json').packageManager || ''" | sed -e 's/^pnpm@//' -e 's/+.*$//')"
if [ -z "$pnpm_version" ]; then
    pnpm_version="__PNPM_FALLBACK__"
fi
[[ "$pnpm_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "prepare.sh: packageManager is not an exact pnpm version: $pnpm_version"; exit 1; }
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
test -x /home/__REPO__/node_modules/.bin/ava || { echo "prepare.sh: ava is not installed"; exit 1; }
node --import=tsx --input-type=module -e "process.exit(0)" || { echo "prepare.sh: tsx does not load"; exit 1; }
/home/__REPO__/node_modules/.bin/ava --version || { echo "prepare.sh: ava does not run"; exit 1; }
echo "gate: Node __NODE_VERSION__, pnpm from packageManager (fallback __PNPM_FALLBACK__), frozen lockfile, ava and tsx load"
"""

_AVA_SH = r"""#!/bin/bash
set -uo pipefail

cd /home/__REPO__
for f in "$@"; do
    if [ ! -f "$f" ]; then
        echo "mswb: $f is not in the tree yet"
        continue
    fi
    out="/tmp/mswb_ava_$(echo "$f" | tr '/.' '__').txt"
    rm -f "$out"
    rc=0
    timeout -k 10 1800 node_modules/.bin/ava "$f" > "$out" < /dev/null || rc=$?
    cat "$out"
    echo "mswb: ava exit $rc for $f"
    node /home/mswb_ava_digest.js "$out" "$f" "$rc" || exit 1
done
exit 0
"""

_AVA_DIGEST_JS = r"""const fs = require('fs');

const out = process.argv[2];
const rel = process.argv[3];
const rc = Number(process.argv[4] || 0);
const crash = rel + '::<suite failed to run> FAILED\n';
if (!fs.existsSync(out)) {
  console.log('mswb: ava wrote no output for ' + rel);
  if (rc !== 0) {
    fs.appendFileSync('/tmp/mswb_results.txt', crash);
  }
  process.exit(0);
}
let crashed = false;
const text = fs.readFileSync(out, 'utf8').replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '').replace(/\r/g, '');
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const duration = / \((?:\d+(?:\.\d+)?(?:ms|s|m|h) ?)+\)$/;
const record = (title, st) => {
  const t = title.replace(/\s+/g, ' ').trim() || '(untitled)';
  const key = rel + '::' + t;
  if (!status.has(key) || rank[st] > rank[status.get(key)]) {
    status.set(key, st);
  }
};
const all = text.split('\n');
const sep = all.findIndex((l) => l.trim() === '─');
const headers = [];
if (sep >= 0) {
  for (const l of all.slice(sep + 1)) {
    const h = /^ {2}(\S.*)$/.exec(l.trimEnd());
    if (h) {
      headers.push(h[1]);
    }
  }
}
const title = (raw) => {
  let best = null;
  for (const h of headers) {
    if ((raw === h || raw.startsWith(h + ' ')) && (best === null || h.length > best.length)) {
      best = h;
    }
  }
  return best === null ? raw : best;
};
for (const line of all) {
  let m;
  if ((m = /^ {2}✔ (?:\[expected fail\]: )?(.+)$/.exec(line))) {
    record(m[1].replace(duration, ''), 'PASSED');
  } else if ((m = /^ {2}✘ \[(?:fail|unexpected pass)\]: (.+)$/.exec(line))) {
    record(title(m[1].trimEnd()), 'FAILED');
  } else if ((m = /^ {2}- \[(?:skip|todo)\] (.+)$/.exec(line))) {
    record(m[1], 'SKIPPED');
  } else if (/^ {2}✘ /.test(line)) {
    console.log('mswb: ava note for ' + rel + ': ' + line.trim());
    crashed = true;
  }
}
if (status.size === 0) {
  console.log('mswb: suite error in ' + rel + ': ava listed no tests');
}
const lines = [...status.keys()].sort().map((key) => key + ' ' + status.get(key) + '\n');
if (crashed || (status.size === 0 && rc !== 0) || rc > 1) {
  lines.push(crash);
}
fs.appendFileSync('/tmp/mswb_results.txt', lines.join(''));
console.log('mswb: ava reported ' + status.size + ' tests for ' + rel);
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


class NanocoderImageBase2367(Image):
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


class NanocoderImageDefault2367(Image):
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
        return NanocoderImageBase2367(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__NODE_VERSION__": _NODE_VERSION,
            "__PNPM_FALLBACK__": _PNPM_FALLBACK,
        }

    def _act(self, name: str, *patches: str) -> File:
        lines = [_RUN_HEADER, "", f"cd /home/{self.pr.repo}", "git update-index -q --refresh", "rm -f /tmp/mswb_results.txt"]
        for patch in patches:
            lines.append(
                f"git apply --whitespace=nowarn {patch} || git apply --3way --whitespace=nowarn {patch} || "
                f'{{ echo "{name}: git apply {patch} failed" >&2; exit 1; }}'
            )
        files = _test_files(self.pr)
        if files:
            lines.append(f"bash /home/mswb_ava.sh {' '.join(files)}")
        lines.append("bash /home/mswb_report.sh")
        return File(".", name, "\n".join(lines) + "\n")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", _text_only(self.pr.fix_patch)),
            File(".", "test.patch", _text_only(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_ava.sh", _render(_AVA_SH, values)),
            File(".", "mswb_ava_digest.js", _AVA_DIGEST_JS),
            File(".", "mswb_report.sh", _REPORT_SH),
            File(".", "prepare.sh", _render(_PREPARE_SH, values)),
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


@Instance.register("Nano-Collective", _INTERVAL)
class NANOCODER_2367_TO_231(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return NanocoderImageDefault2367(self.pr, self._config)

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
