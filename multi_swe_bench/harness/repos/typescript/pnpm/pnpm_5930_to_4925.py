import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "pnpm"
_REPO = "pnpm"

_PR_NUMBERS = (
    "5930",
    "5906",
    "5897",
    "5856",
    "5847",
    "5829",
    "5331",
    "5263",
    "5067",
    "4925",
)

_NODE_IMAGE = "node:16-bullseye"

_PNPM_VERSION = "7.33.7"

_ENV_BLOCK = """ENV NPM_CONFIG_PROGRESS=false \\
    NPM_CONFIG_COLOR=false \\
    NO_COLOR=1 \\
    FORCE_COLOR=0 \\
    CI=true \\
    YARN_NETWORK_TIMEOUT=1800000 \\
    NPM_CONFIG_FETCH_TIMEOUT=1800000 \\
    NPM_CONFIG_FETCH_RETRIES=10 \\
    NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000 \\
    NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=600000"""

_TS_JEST_GLOBALS = "'{\"ts-jest\":{\"diagnostics\":false}}'"

_JEST_HEAP_MB = "4096"

_JSON_BEGIN = "===== JEST-JSON-BEGIN"
_JSON_END = "===== JEST-JSON-END"

_TEST_DIR_RE = re.compile(r"^diff --git a/(.+?)/test/\S+", re.M)
_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+)", re.M)


def _is_candidate_dir(path: str) -> bool:
    return bool(path) and not path.startswith(".") and path.count("/") <= 1


def _test_dirs(pr: PullRequest) -> list[str]:
    """Package directories whose tests this PR touches -- where jest is run.

    Derived from the PR's own test patch rather than hardcoded, because the ten
    PRs here hit very different parts of the workspace: 5856 is only
    exec/plugin-commands-script-runners, while 5897 spans two fetchers, a
    rebuild command and pkg-manager/core.

    A directory that does not exist at the base commit is not dropped here --
    the scripts skip it at run time instead. PR 5829's
    config/plugin-commands-config is created by its own fix patch, so it is
    absent in the run and test stages and present in the fix stage, which is
    exactly the none-to-passed shape a new package should produce.
    """
    return sorted(
        {d for d in _TEST_DIR_RE.findall(pr.test_patch or "") if _is_candidate_dir(d)}
    )


def _compile_dirs(pr: PullRequest) -> list[str]:
    """Directories to rebuild: the graded ones plus everything the patches edit.

    This matters more than it looks. Every package declares "main": "lib/index.js"
    and cross-package imports resolve through the workspace symlink to that
    compiled output, not to src. A fix patch edits src/. Without a rebuild after
    the patch is applied the fix is simply invisible to the tests, and the
    instance reads as unresolved with nothing in the log to explain why.

    Candidates are the first one and first two segments of every changed path,
    so both `pnpm/src/pnpm.ts` -> `pnpm` and
    `pkg-manager/package-requester/src/packageRequester.ts` ->
    `pkg-manager/package-requester` are covered. The category directories that
    fall out of this (exec, store, packages, ...) carry no package.json and are
    skipped by compile.sh at run time.
    """
    dirs = set(_test_dirs(pr))
    for path in _DIFF_FILE_RE.findall(pr.fix_patch or "") + _DIFF_FILE_RE.findall(
        pr.test_patch or ""
    ):
        if path.startswith(".") or "/" not in path:
            continue
        parts = path.split("/")
        if _is_candidate_dir(parts[0]):
            dirs.add(parts[0])
        if len(parts) > 2:
            dirs.add("/".join(parts[:2]))
    return sorted(dirs)


def _sh_list(names: list[str]) -> str:
    return " ".join(f'"{n}"' for n in names)


def _render(template: str, **values: str) -> str:
    for key, value in values.items():
        template = template.replace(f"@@{key}@@", value)
    return template


_BASE_SEEDS: dict[str, PullRequest] = {}


def _register_base_seed(pr: PullRequest) -> None:
    key = f"{pr.org}/{pr.repo}"
    current = _BASE_SEEDS.get(key)
    if current is None or pr.number > current.number:
        _BASE_SEEDS[key] = pr


def _base_seed(pr: PullRequest) -> PullRequest:
    return _BASE_SEEDS.get(f"{pr.org}/{pr.repo}", pr)


_HARDENING = '''RUN set -eux; \\
    git checkout --detach "@@SHA@@"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "@@SHA@@")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi'''


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


_LINK_SELF_SH = """#!/bin/bash
set -e

cd /home/@@REPO@@

pnpm list --recursive --depth -1 --json > /tmp/workspace_pkgs.json 2>/dev/null || echo "[]" > /tmp/workspace_pkgs.json

node -e '
const fs = require("fs");
const path = require("path");
const root = process.argv[1];

let pkgs = [];
try {
  pkgs = JSON.parse(fs.readFileSync("/tmp/workspace_pkgs.json", "utf8"));
} catch (err) {
  pkgs = [];
}
if (!Array.isArray(pkgs)) pkgs = [];

if (pkgs.length === 0) {
  const walk = (dir, depth) => {
    if (depth > 2) return;
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (err) {
      return;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      if (entry.name === "node_modules" || entry.name === ".git") continue;
      if (entry.name === "test" || entry.name === "src" || entry.name === "lib") continue;
      const sub = path.join(dir, entry.name);
      if (fs.existsSync(path.join(sub, "package.json"))) pkgs.push({ path: sub });
      walk(sub, depth + 1);
    }
  };
  walk(root, 1);
}

for (const pkg of pkgs) {
  const dir = pkg.path;
  if (!dir || path.resolve(dir) === path.resolve(root)) continue;
  let name = pkg.name;
  if (!name) {
    try {
      name = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8")).name;
    } catch (err) {
      continue;
    }
  }
  if (!name) continue;
  const link = path.join(dir, "node_modules", name);
  if (fs.existsSync(link)) continue;
  try {
    fs.mkdirSync(path.dirname(link), { recursive: true });
    fs.symlinkSync(dir, link, "dir");
  } catch (err) {
  }
}
' /home/@@REPO@@
"""


_COMPILE_SH = """#!/bin/bash

cd /home/@@REPO@@

set -- @@COMPILE_DIRS@@

rm -f /tmp/cli_bundle_required
for dir in @@TEST_DIRS@@; do
    if [ -d "$dir/test" ] && grep -rqF -- "pnpm/bin/pnpm.cjs" "$dir/test" 2>/dev/null; then
        touch /tmp/cli_bundle_required
        break
    fi
done

if [ -f /tmp/cli_bundle_required ] && [ -f pnpm/package.json ]; then
    has_cli=0
    for dir in "$@"; do
        if [ "$dir" = "pnpm" ]; then
            has_cli=1
        fi
    done
    if [ "$has_cli" = 0 ]; then
        set -- "$@" pnpm
    fi
fi

for dir in "$@"; do
    if [ ! -f "$dir/package.json" ]; then
        continue
    fi
    (
        set -e
        cd "$dir"
        pnpm exec tsc --build
        if node -e "var s=require('./package.json').scripts||{};process.exit(s.bundle?0:1)"; then
            pnpm run bundle
            if [ -d node-gyp-bin ]; then
                rm -rf dist/node-gyp-bin
                cp -r node-gyp-bin dist/node-gyp-bin
            fi
            if [ -d node_modules/@pnpm/tabtab/lib/scripts ]; then
                rm -rf dist/scripts
                cp -r node_modules/@pnpm/tabtab/lib/scripts dist/scripts
            fi
            if [ -d node_modules/ps-list/vendor ]; then
                rm -rf dist/vendor
                cp -r node_modules/ps-list/vendor dist/vendor
            fi
            if [ -f pnpmrc ]; then
                cp pnpmrc dist/pnpmrc
            fi
        fi
    ) || true
done
"""


_PREPARE_SH = """#!/bin/bash
set -e

cd /home/@@REPO@@
bash /home/check_git_changes.sh

pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile
git checkout -- .
git diff --quiet
bash /home/link_self.sh

for fixtures in __fixtures__ fixtures; do
    if [ -d "/home/@@REPO@@/$fixtures" ]; then
        pnpm --dir="/home/@@REPO@@/$fixtures" run prepareFixtures || true
    fi
done

cd /home/@@REPO@@
pnpm exec registry-mock prepare > /dev/null 2>&1 || true

bash /home/compile.sh

# compile.sh leaves this stamp when a graded test spawns the real CLI. The
# bundle it then builds is the difference between those tests running and every
# one of them dying on `Cannot find module '../dist/pnpm.cjs'`, so refuse to
# seal an image that is missing it.
if [ -f /tmp/cli_bundle_required ]; then
    test -f /home/@@REPO@@/pnpm/dist/pnpm.cjs
fi

graded=0
for dir in @@TEST_DIRS@@; do
    if [ ! -f "/home/@@REPO@@/$dir/package.json" ]; then
        echo "prepare: $dir does not exist at the base commit, skipping"
        continue
    fi
    cd "/home/@@REPO@@/$dir"
    export PNPM_SCRIPT_SRC_DIR="/home/@@REPO@@/$dir"
    pnpm exec jest --version > /dev/null
    pnpm exec jest --listTests > /tmp/listtests.txt 2>&1
    test -s /tmp/listtests.txt
    wc -l < /tmp/listtests.txt
    node -e "var p=require('./package.json');var d=Object.assign({},p.dependencies,p.devDependencies);if(d[p.name])require.resolve(p.name)"
    if node -e "var s=require('./package.json').scripts||{};process.exit(s.bundle?0:1)"; then
        test -d dist
    fi
    graded=$((graded+1))
done

if [ "$graded" -lt 1 ]; then
    echo "prepare: no graded package exists at the base commit" >&2
    exit 1
fi
"""


_GRADED_SH = """cd /home/@@REPO@@

pnpm install --no-frozen-lockfile || true
bash /home/link_self.sh
bash /home/compile.sh

for dir in @@TEST_DIRS@@; do
    if [ ! -f "/home/@@REPO@@/$dir/package.json" ]; then
        continue
    fi
    cd "/home/@@REPO@@/$dir"
    slug=$(echo "$dir" | tr '/' '_')
    port=$(node -e "var p=require('./package.json');var s=(p.scripts&&p.scripts._test)||'';var m=s.match(/PNPM_REGISTRY_MOCK_PORT=([0-9]+)/);process.stdout.write(m?m[1]:'')")
    mock_pid=""
    if [ -n "$port" ]; then
        export PNPM_REGISTRY_MOCK_PORT="$port"
        pnpm exec registry-mock prepare > /dev/null 2>&1 || true
        pnpm exec registry-mock > /dev/null 2>&1 &
        mock_pid=$!
        for i in $(seq 1 60); do
            if node -e "require('net').connect($port,'127.0.0.1').on('connect',function(){process.exit(0)}).on('error',function(){process.exit(1)})" > /dev/null 2>&1; then
                break
            fi
            sleep 1
        done
    else
        unset PNPM_REGISTRY_MOCK_PORT
    fi
    export PNPM_SCRIPT_SRC_DIR="/home/@@REPO@@/$dir"
    NODE_OPTIONS="--max-old-space-size=@@JEST_HEAP_MB@@" \\
    pnpm exec jest --ci --runInBand --coverage=false \\
        --globals @@TS_JEST_GLOBALS@@ \\
        --json --outputFile="/tmp/jest_$slug.json" > /dev/null 2>&1 || true
    if [ -n "$mock_pid" ]; then
        kill "$mock_pid" > /dev/null 2>&1 || true
    fi
    echo "@@JSON_BEGIN@@ $dir ====="
    cat "/tmp/jest_$slug.json" 2>/dev/null || echo "{}"
    echo ""
    echo "@@JSON_END@@ $dir ====="
done"""


class Pnpm5930To4925ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return _base_seed(self._pr)

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-5930-to-4925"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'

        return f"""FROM {image_name}

{self.global_env}

{_ENV_BLOCK}

RUN npm install -g pnpm@{_PNPM_VERSION} --no-audit --no-fund

WORKDIR /home/

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Pnpm5930To4925ImageDefault(Image):
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
        return Pnpm5930To4925ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _graded_command(self) -> str:
        return _render(
            _GRADED_SH,
            REPO=self.pr.repo,
            TEST_DIRS=_sh_list(_test_dirs(self.pr)),
            TS_JEST_GLOBALS=_TS_JEST_GLOBALS,
            JEST_HEAP_MB=_JEST_HEAP_MB,
            JSON_BEGIN=_JSON_BEGIN,
            JSON_END=_JSON_END,
        )

    def files(self) -> list[File]:
        cmd = self._graded_command()
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "link_self.sh", _render(_LINK_SELF_SH, REPO=repo)),
            File(
                ".",
                "compile.sh",
                _render(
                    _COMPILE_SH,
                    REPO=repo,
                    COMPILE_DIRS=_sh_list(_compile_dirs(self.pr)),
                    TEST_DIRS=_sh_list(_test_dirs(self.pr)),
                ),
            ),
            File(
                ".",
                "prepare.sh",
                _render(_PREPARE_SH, REPO=repo, TEST_DIRS=_sh_list(_test_dirs(self.pr))),
            ),
            File(".", "run.sh", f"#!/bin/bash\n{cmd}\n"),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
if ! git -C /home/{repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = _render(_HARDENING, SHA=self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{hardening}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register(_ORG, _REPO)
class Pnpm5930To4925(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _register_base_seed(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Pnpm5930To4925ImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        repo_prefix = f"/home/{self.pr.repo}/"

        blob_re = re.compile(
            re.escape(_JSON_BEGIN)
            + r"\s+(?P<pkg>\S+)\s+=====\s*(?P<body>.*?)"
            + re.escape(_JSON_END),
            re.S,
        )

        for match in blob_re.finditer(log):
            body = match.group("body")
            start = body.find("{")
            if start == -1:
                continue

            try:
                data = json.loads(body[start:])
            except Exception:
                end = body.rfind("}")
                if end <= start:
                    continue
                try:
                    data = json.loads(body[start : end + 1])
                except Exception:
                    continue

            for suite in data.get("testResults") or []:
                path = suite.get("name") or ""
                if repo_prefix in path:
                    path = path.split(repo_prefix, 1)[1]
                path = path.replace("\\", "/")

                assertions = suite.get("assertionResults") or []

                if not assertions:
                    if suite.get("status") == "failed" or suite.get("message"):
                        failed_tests.add(f"{path}::<suite failed to load>")
                    continue

                for assertion in assertions:
                    name = assertion.get("fullName") or assertion.get("title") or ""
                    if not name:
                        continue
                    test_id = f"{path}::{name}" if path else name
                    status = (assertion.get("status") or "").lower()

                    if status == "passed":
                        passed_tests.add(test_id)
                    elif status == "failed":
                        failed_tests.add(test_id)
                    else:
                        skipped_tests.add(test_id)

        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


for _pr_number in _PR_NUMBERS:
    Instance.register(_ORG, _pr_number)(Pnpm5930To4925)
