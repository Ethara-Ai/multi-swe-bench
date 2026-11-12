import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:22.16.0-bookworm-slim"
_NODE_VERSION = "22.16.0"
_COREPACK_VERSION = "0.36.0"
_BASE_TAG = "base-1577_to_4662"
_PLAYWRIGHT_PATH = "/root/.cache/ms-playwright"
_BROWSER_PACKAGES = {"packages/admin"}
_TYPES_CONFIG = "vitest.types.config.ts"
_REPRO_CONFIG = "vitest.repro.config.ts"
_DEAD_ROOT_DEPS = {
    1577: ["@lunariajs/core"],
    3176: ["@lunariajs/core"],
}
_FIX_LINKS = {
    4662: [("packages/core", "@portabletext/types", "2.0.15")],
}

_TEST_RE = re.compile(
    r"^\+\+\+ b/(packages/[^/\s]+)/(tests/\S+\.test(?:-d)?\.[cm]?[jt]sx?)$",
    re.MULTILINE,
)


def _config_for(test_file: str) -> str:
    if test_file.endswith((".test-d.ts", ".test-d.tsx")):
        return _TYPES_CONFIG
    if test_file.startswith("tests/repro/") and ".render.test." in test_file:
        return _REPRO_CONFIG
    return "-"


def _groups(pr: PullRequest) -> list[tuple[str, str, list[str]]]:
    grouped: dict[tuple[str, str], set[str]] = {}
    for package, test_file in _TEST_RE.findall(pr.test_patch or ""):
        key = (package, _config_for(test_file))
        grouped.setdefault(key, set()).add(test_file)
    return [(pkg, cfg, sorted(files)) for (pkg, cfg), files in sorted(grouped.items())]


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

[ "$(node -v)" = "v__NODE_VERSION__" ] || { echo "prepare.sh: Node __NODE_VERSION__ is not active"; exit 1; }
pm="$(node -p "require('./package.json').packageManager")"
want="${pm#pnpm@}"
want="${want%%+*}"
[ "$(pnpm --version)" = "$want" ] || { echo "prepare.sh: pnpm $want is not active"; exit 1; }
__DROP_DEPS__
for attempt in 1 2 3; do
    pnpm install --frozen-lockfile && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: pnpm install failed 3 times"; exit 1; }
    sleep 30
done

pnpm run --if-present __BUILD_FILTERS__ build
__LINKS__
__BROWSER__
__GATES__
node -e "for (const p of process.argv.slice(1)) require('/home/__REPO__/' + p + '/node_modules/vitest/package.json')" __PNPM_LIST__
echo "gate: pnpm __PNPM_LIST__ installed, built, and the PR test files load"
"""

_LINK_SNIPPET = r"""src="$(ls -d /home/__REPO__/node_modules/.pnpm/__STORE__@__VERSION__/node_modules/__NAME__ 2>/dev/null | head -n 1)"
[ -n "$src" ] && [ -f "$src/package.json" ] || { echo "prepare.sh: __NAME__@__VERSION__ is not in the pnpm store"; exit 1; }
[ "$(node -p "require('$src/package.json').version")" = "__VERSION__" ] || { echo "prepare.sh: __NAME__ store copy is not __VERSION__"; exit 1; }
mkdir -p "/home/__REPO__/__PKG__/node_modules/__SCOPE__"
ln -sfn "$src" "/home/__REPO__/__PKG__/node_modules/__NAME__"
"""

_DROP_DEP_SNIPPET = r"""echo "prepare.sh: __DEP__ is a pkg.pr.new preview build whose tarball returns 404; removing it from the root package.json and pnpm-lock.yaml"
node - "__DEP__" <<'JS'
const fs = require('fs');
const dep = process.argv[2];
const pj = JSON.parse(fs.readFileSync('package.json', 'utf8'));
if (!pj.devDependencies || !(dep in pj.devDependencies)) {
  console.error('prepare.sh: ' + dep + ' is not a root devDependency');
  process.exit(1);
}
delete pj.devDependencies[dep];
fs.writeFileSync('package.json', JSON.stringify(pj, null, '\t') + '\n');
const lines = fs.readFileSync('pnpm-lock.yaml', 'utf8').split('\n');
const indent = (s) => s.length - s.trimStart().length;
const out = [];
let skip = -1;
for (const line of lines) {
  if (skip >= 0) {
    if (line.trim() === '') {
      const top = skip === 2;
      skip = -1;
      if (top) continue;
      out.push(line);
      continue;
    }
    if (indent(line) > skip) continue;
    skip = -1;
  }
  const t = line.trim();
  if (t.endsWith(':') && (t === "'" + dep + "':" || t.startsWith("'" + dep + '@'))) {
    skip = indent(line);
    continue;
  }
  out.push(line);
}
fs.writeFileSync('pnpm-lock.yaml', out.join('\n'));
JS
if grep -qF "__DEP__" package.json pnpm-lock.yaml; then
    echo "prepare.sh: __DEP__ is still referenced after removal"
    exit 1
fi
"""

_BROWSER_SNIPPET = r"""cd /home/__REPO__/__PKG__
for attempt in 1 2 3; do
    node_modules/.bin/playwright install --with-deps chromium && break
    [ "$attempt" -lt 3 ] || { echo "prepare.sh: playwright chromium install failed 3 times"; exit 1; }
    sleep 30
done
rm -rf /var/lib/apt/lists/*
chrome="$(node -e "console.log(require('playwright').chromium.executablePath())")"
test -x "$chrome" || { echo "prepare.sh: chromium is missing at $chrome"; exit 1; }
"""

_GATE_SNIPPET = r"""cd /home/__REPO__/__PKG__
test -x node_modules/.bin/vitest || { echo "prepare.sh: vitest is not installed in __PKG__"; exit 1; }
files=()
for f in __FILES__; do
    if [ -f "$f" ]; then
        files+=("$f")
    fi
done
if [ ${#files[@]} -gt 0 ]; then
    node_modules/.bin/vitest list --filesOnly __CONFIG_ARG__"${files[@]}" > /tmp/mswb_collect___N__.log
    tail -n 1 /tmp/mswb_collect___N__.log
fi
"""

_VITEST_SH = r"""#!/bin/bash
set -uo pipefail

pkg="$1"
config="$2"
shift 2
cd "/home/__REPO__/$pkg"
out="/tmp/mswb_vitest_$(echo "$pkg-$config" | tr '/.' '__').json"
files=()
for f in "$@"; do
    if [ -f "$f" ]; then
        files+=("$f")
    else
        echo "mswb: $pkg/$f is not in the tree yet"
    fi
done
rm -f "$out"
if [ ${#files[@]} -eq 0 ]; then
    echo "mswb: none of the $pkg test files for config $config exist yet"
    exit 0
fi

args=()
if [ "$config" != "-" ]; then
    args+=(--config "$config")
fi

rc=0
node_modules/.bin/vitest run "${args[@]}" --reporter=json --outputFile="$out" "${files[@]}" < /dev/null || rc=$?
echo "mswb: $pkg vitest ($config) exit $rc"
node /home/mswb_digest.js "$out" || exit 1
if [ "$rc" -gt 1 ]; then
    echo "=== MSB: $pkg vitest ($config) exited with status $rc ==="
    exit "$rc"
fi
if [ ! -f "$out" ] && [ "$rc" -ne 0 ]; then
    echo "=== MSB: $pkg vitest ($config) wrote no report (status $rc) ==="
    exit 1
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
const root = '/home/__REPO__';
const rank = { PASSED: 0, SKIPPED: 1, FAILED: 2 };
const status = new Map();
const report = JSON.parse(fs.readFileSync(out, 'utf8'));
for (const file of report.testResults || []) {
  const rel = path.relative(root, file.name);
  const results = file.assertionResults || [];
  for (const t of results) {
    const title = [...(t.ancestorTitles || []), t.title].map((x) => String(x).replace(/\s+/g, ' ').trim()).join(' > ');
    const st = t.status === 'passed' ? 'PASSED' : t.status === 'failed' ? 'FAILED' : 'SKIPPED';
    const key = 'vitest::' + rel + '::' + title;
    if (!status.has(key) || rank[st] > rank[status.get(key)]) {
      status.set(key, st);
    }
  }
  if (results.length === 0 && file.status === 'failed') {
    console.log('mswb: suite error in ' + rel + ': ' + String(file.message || '').split('\n')[0]);
  }
}
for (const key of [...status.keys()].sort()) {
  console.log(key + ' ' + status.get(key));
}
"""

_RUN_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true"""


def _render(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(key, value)
    return template


class EmdashImageBase(Image):
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
    git build-essential python3 ca-certificates curl xz-utils \\
    && rm -rf /var/lib/apt/lists/*

RUN [ "$(node -v)" = "v{_NODE_VERSION}" ] || {{ echo "node {_NODE_VERSION} missing"; exit 1; }}

RUN npm install -g corepack@{_COREPACK_VERSION} && \\
    corepack enable && \\
    [ "$(corepack --version)" = "{_COREPACK_VERSION}" ]

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class EmdashImageDefault(Image):
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
        return EmdashImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _links(self) -> str:
        parts = []
        for pkg, name, version in _FIX_LINKS.get(self.pr.number, []):
            scope = name.split("/")[0] if name.startswith("@") else ""
            parts.append(
                _render(
                    _LINK_SNIPPET,
                    {
                        "__STORE__": name.replace("/", "+"),
                        "__VERSION__": version,
                        "__NAME__": name,
                        "__PKG__": pkg,
                        "__SCOPE__": scope,
                    },
                )
            )
        return "".join(parts)

    def _browser(self, groups: list[tuple[str, str, list[str]]]) -> str:
        packages = sorted({pkg for pkg, _, _ in groups if pkg in _BROWSER_PACKAGES})
        return "".join(_render(_BROWSER_SNIPPET, {"__PKG__": pkg}) for pkg in packages)

    def _gates(self, groups: list[tuple[str, str, list[str]]]) -> str:
        parts = []
        for n, (pkg, cfg, files) in enumerate(groups):
            if pkg in _BROWSER_PACKAGES:
                parts.append(
                    f"test -x /home/{self.pr.repo}/{pkg}/node_modules/.bin/vitest || "
                    f'{{ echo "prepare.sh: vitest is not installed in {pkg}"; exit 1; }}\n'
                )
                continue
            config_arg = "" if cfg == "-" else f"--config {cfg} "
            parts.append(
                _render(
                    _GATE_SNIPPET,
                    {
                        "__PKG__": pkg,
                        "__FILES__": " ".join(files),
                        "__CONFIG_ARG__": config_arg,
                        "__N__": str(n),
                    },
                )
            )
        return "".join(parts)

    def _drop_deps(self) -> str:
        return "".join(
            _render(_DROP_DEP_SNIPPET, {"__DEP__": dep})
            for dep in _DEAD_ROOT_DEPS.get(self.pr.number, [])
        )

    def _build_filters(self, groups: list[tuple[str, str, list[str]]]) -> str:
        packages = sorted({pkg for pkg, _, _ in groups})
        return " ".join(f'--filter "{{./{pkg}}}..."' for pkg in packages)

    def _values(self) -> dict[str, str]:
        return {
            "__REPO__": self.pr.repo,
            "__SHA__": self.pr.base.sha,
            "__NODE_VERSION__": _NODE_VERSION,
        }

    def _prepare(self) -> str:
        groups = _groups(self.pr)
        packages = sorted({pkg for pkg, _, _ in groups})
        text = _render(
            _PREPARE_SH,
            {
                "__DROP_DEPS__": self._drop_deps(),
                "__LINKS__": self._links(),
                "__BUILD_FILTERS__": self._build_filters(groups),
                "__BROWSER__": self._browser(groups),
                "__GATES__": self._gates(groups),
                "__PNPM_LIST__": " ".join(packages),
            },
        )
        return _render(text, self._values())

    def _test_cmds(self) -> str:
        lines = [f"cd /home/{self.pr.repo}"]
        for pkg, cfg, files in _groups(self.pr):
            lines.append(f"bash /home/mswb_vitest.sh {pkg} {cfg} {' '.join(files)}")
        return "\n".join(lines) + "\n"

    def _act(self, name: str, *patches: str) -> File:
        apply = ""
        if patches:
            apply = (
                f"cd /home/{self.pr.repo}\n"
                f"git apply --whitespace=nowarn {' '.join(patches)} || "
                f'{{ echo "{name}: git apply failed" >&2; exit 1; }}\n'
            )
        return File(".", name, f"{_RUN_HEADER}\n\n{apply}{self._test_cmds()}")

    def files(self) -> list[File]:
        values = self._values()
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_digest.js", _render(_DIGEST_JS, values)),
            File(".", "mswb_vitest.sh", _render(_VITEST_SH, values)),
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
    HUSKY=0 \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    PLAYWRIGHT_BROWSERS_PATH={_PLAYWRIGHT_PATH} \\
    ASTRO_TELEMETRY_DISABLED=1 \\
    THREADS_WORKER_INIT_TIMEOUT=120000 \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_LINE_RE = re.compile(
    r"^(?P<name>vitest::packages/[^/\s]+/tests/\S.*?\.test(?:-d)?\.[cm]?[jt]sx?(?:::.+)?) (?P<status>PASSED|FAILED|SKIPPED)$"
)


@Instance.register("emdash-cms", "emdash")
class EMDASH(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return EmdashImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        found: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "SKIPPED": set()}
        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            m = _LINE_RE.match(line.rstrip())
            if m:
                found[m.group("status")].add(m.group("name").strip())

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
