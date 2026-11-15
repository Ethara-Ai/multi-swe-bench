import posixpath
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "freeCodeCamp"
_REPO = "freeCodeCamp"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"freecodecamp_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like freecodecamp_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# Every base commit of this era (2026-07-24 .. 2026-09-14) declares
# "packageManager": "pnpm@10.33.3", engines.node ">=24" and .nvmrc "24".
NODE_IMAGE = "node:24.22.0-bookworm"
PNPM_VERSION = "10.33.3"

# pnpm workspaces whose vitest suites the test patches of this era touch:
# workspace directory -> package name (the --filter target).
_WORKSPACES = {
    "api": "@freecodecamp/api",
    "client": "@freecodecamp/client",
    "curriculum": "@freecodecamp/curriculum",
    "packages/challenge-builder": "@freecodecamp/challenge-builder",
    "tools/challenge-helper-scripts": "@freecodecamp/curriculum-helper-scripts",
}

# tools/challenge-parser is imported by relative path (not declared as a dependency)
# from the curriculum build and the helper scripts, so it is always installed too.
_ALWAYS_INSTALL = ["@freecodecamp/challenge-parser"]

# #69474 reverts the React 19 upgrade (#69271, 3746de82cf): its fix patch restores
# client/package.json and pnpm-lock.yaml of 3746de82cf~1 byte for byte. The fix stage
# therefore has to reinstall the client dependencies, and there is no network at grade
# time, so the pnpm store is warmed at build time with that ancestor's dependency set
# (a commit already in the base commit's history; nothing from the fix is read).
_STORE_WARM_COMMITS = {
    69474: "397e04dda353327253c799f231c3561cb8d46a24",
}

_TEST_FILE_RE = re.compile(r"\.test\.(?:js|jsx|mjs|cjs|ts|tsx|mts|cts)$")
_CHALLENGE_MD_RE = re.compile(r"^curriculum/challenges/[^/]+/blocks/([^/]+)/[^/]+\.md$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_COMMON_ENV = (
    "# The dummy settings CI loads from sample.env (api/src/utils/env.ts asserts them,\n"
    "# the curriculum build needs CURRICULUM_LOCALE).\n"
    "set -a\n"
    ". ./sample.env\n"
    "set +a\n"
    "export TURBO_TELEMETRY_DISABLED=1 DO_NOT_TRACK=1 PUPPETEER_SKIP_DOWNLOAD=true\n"
)

_VITEST_REPORT_CJS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('vitest-report: no results file at ' + path + ' -- vitest produced no output');
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('vitest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED'};

for (const suite of report.testResults || []) {
    let file = String(suite.name || '').replace(/\\/g, '/');
    if (file.startsWith(ROOT)) file = file.slice(ROOT.length);

    const results = suite.assertionResults || [];
    if (results.length === 0 && suite.status === 'failed') {
        console.log('FAILED ' + file + ' > <suite failed to run>');
        continue;
    }
    const seen = new Map();
    for (const a of results) {
        let name = [file].concat(a.ancestorTitles || [], [a.title]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        console.log((STATUS[a.status] || 'SKIPPED') + ' ' + name);
    }
}

console.log('vitest-report: suites=' + (report.numTotalTestSuites || 0) +
            ' tests=' + (report.numTotalTests || 0) +
            ' passed=' + (report.numPassedTests || 0) +
            ' failed=' + (report.numFailedTests || 0) +
            ' pending=' + (report.numPendingTests || 0));
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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


def _patch_chunks(patch: str) -> list[tuple[str, bool]]:
    chunks = []
    for chunk in re.split(r"^(?=diff --git )", patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match:
            chunks.append((match.group(1), re.search(r"^new file mode ", chunk, re.M) is not None))
    return chunks


def _workspace_of(path: str) -> Optional[str]:
    best = None
    for ws in _WORKSPACES:
        if path.startswith(ws + "/") and (best is None or len(ws) > len(best)):
            best = ws
    return best


def _test_groups(pr: PullRequest) -> dict[str, list[str]]:
    """Workspace dir -> candidate test paths (relative to it) the test patch is about.

    Vitest files the patch touches are run directly. A curriculum challenge .md is
    exercised by the generated block suite src/test/blocks-generated/<block>.test.js.
    Playwright e2e specs (*.spec.ts) need the full client + api + MongoDB stack and
    are not run.
    """
    chunks = _patch_chunks(pr.test_patch)
    groups: dict[str, set[str]] = {}
    for path, is_new in chunks:
        md = _CHALLENGE_MD_RE.match(path)
        if md:
            groups.setdefault("curriculum", set()).add(
                f"src/test/blocks-generated/{md.group(1)}.test.js"
            )
            continue
        ws = _workspace_of(path)
        if ws is None or not _TEST_FILE_RE.search(path):
            continue
        groups.setdefault(ws, set()).add(path[len(ws) + 1:])
    return {ws: sorted(files) for ws, files in sorted(groups.items())}


def _only_new_test_files(pr: PullRequest, ws: str) -> bool:
    flags = [
        is_new
        for path, is_new in _patch_chunks(pr.test_patch)
        if path.startswith(ws + "/") and _TEST_FILE_RE.search(path)
    ]
    return bool(flags) and all(flags)


def _candidates(pr: PullRequest, ws: str, files: list[str]) -> str:
    # A file the patch adds does not exist before it is applied; when every touched
    # file is new, the baseline stage runs the sibling test files of the same
    # directories instead, so run.sh still observes something.
    if not _only_new_test_files(pr, ws):
        return " ".join(files)
    dirs = " ".join(sorted({posixpath.dirname(f) or "." for f in files}))
    pattern = _TEST_FILE_RE.pattern.replace("?:", "")
    return f"$(cd {ws} && find {dirs} -maxdepth 1 -type f | grep -E '{pattern}' | sort)"


def _uses_curriculum_suite(pr: PullRequest) -> bool:
    return "curriculum" in _test_groups(pr)


def _install_filters(pr: PullRequest) -> str:
    pkgs = [_WORKSPACES[ws] for ws in _test_groups(pr)]
    return " ".join(
        [f'--filter "{p}..."' for p in pkgs] + [f'--filter "{p}"' for p in _ALWAYS_INSTALL]
    )


def _slug(ws: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", ws)


def _test_cmd(pr: PullRequest) -> str:
    groups = _test_groups(pr)
    lines = [
        "rm -f /tmp/vitest-*.json",
        "",
        _COMMON_ENV.rstrip("\n"),
        "",
        "# The applied patches may change the dependency set (#69474 reverts a React",
        "# upgrade in pnpm-lock.yaml). Reinstall from the store warmed at build time,",
        "# offline, whenever the lockfile differs from the one last installed.",
        'lock_hash="$(sha256sum pnpm-lock.yaml | cut -d" " -f1)"',
        'if [ "$lock_hash" != "$(cat /home/.pnpm-lock.sha256)" ]; then',
        '    echo "pnpm-lock.yaml changed; reinstalling offline"',
        f"    pnpm install --offline --frozen-lockfile {_install_filters(pr)}",
        '    echo "$lock_hash" > /home/.pnpm-lock.sha256',
        "fi",
        "",
    ]
    if "curriculum" in groups:
        lines += [
            "# Challenge .md files are tested through one generated suite per block.",
            "export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium",
            "(cd curriculum && ../node_modules/.bin/tsx ./src/test/utils/generate-block-tests.ts)",
            "",
        ]
    lines += [
        "# Only the test files the test patch touches; a file the patch adds does not exist",
        "# before it is applied, so each stage runs the ones present at that point.",
    ]
    for ws, files in groups.items():
        slug = _slug(ws)
        out = f"/tmp/vitest-{slug}.json"
        lines += [
            "files=()",
            f"for f in {_candidates(pr, ws, files)}; do",
            f'    if [ -f "{ws}/$f" ]; then files+=("$f"); fi',
            "done",
            'if [ "${#files[@]}" -gt 0 ]; then',
            "    rc=0",
            f"    (cd {ws} && node_modules/.bin/vitest run --reporter=json "
            f'--outputFile={out} "${{files[@]}}") || rc=$?',
            f"    node /home/vitest-report.cjs {out}",
            f"    if [ ! -s {out} ]; then",
            f'        echo "vitest in {ws} exited with code $rc without writing {out}" >&2',
            '        exit "$(( rc == 0 ? 1 : rc ))"',
            "    fi",
            "else",
            f'    echo "no {ws} test file of the test patch exists at this stage"',
            "fi",
            "",
        ]
    return "\n".join(lines)


def _reset_tree() -> str:
    # Repeated calls on one container: drop the previous stage's patches, including
    # files a patch added. Ignored paths (node_modules, built dist/, .env) survive.
    return "git reset --hard\ngit clean -fd -q\n"


class FreeCodeCampImageBase(Image):
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

# Shard-wide toolchain: the pnpm release every base commit of this era pins in
# package.json "packageManager".
RUN npm install -g pnpm@{PNPM_VERSION} && pnpm --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class FreeCodeCampImageDefault(Image):
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
        return FreeCodeCampImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        groups = _test_groups(self.pr)
        filters = _install_filters(self.pr)
        vitest_report = _VITEST_REPORT_CJS.replace("__REPO__", repo)

        provision = [
            "# --- 2. provision ---",
            "# Result reporter the run scripts use; written outside the repo.",
            "cat > /home/vitest-report.cjs <<'__VITEST_REPORT_EOF__'",
            vitest_report.rstrip("\n"),
            "__VITEST_REPORT_EOF__",
            "",
            "node --version",
            "pnpm --version",
            "",
            "# api/src/utils/env.ts loads ../../../.env (gitignored); CI uses sample.env.",
            "cp sample.env .env",
            _COMMON_ENV.rstrip("\n"),
            "",
        ]
        if _uses_curriculum_suite(self.pr):
            provision += [
                "# The curriculum block suites drive challenges in a headless browser via",
                "# puppeteer. Chrome for Testing has no linux/arm64 build, so Debian's",
                "# chromium (amd64 + arm64) is used instead.",
                "apt-get update",
                "apt-get install -y --no-install-recommends chromium",
                "rm -rf /var/lib/apt/lists/*",
                "",
            ]
        provision += [
            "# Only the workspaces whose tests this PR runs, plus their dependencies,",
            "# from the lockfile of this base commit.",
            "for attempt in 1 2 3; do",
            f"    pnpm install --frozen-lockfile {filters} && break || true",
            '    echo "prepare: pnpm install attempt $attempt failed; retrying in 15s"',
            "    sleep 15",
            "done",
            "# Non-tolerant re-check: everything the lockfile needs is now installed.",
            f"pnpm install --offline --frozen-lockfile {filters}",
            "",
        ]
        warm = _STORE_WARM_COMMITS.get(self.pr.number)
        if warm:
            provision += [
                "# Warm the pnpm store with the dependency set of an ancestor commit so the",
                "# fix stage can reinstall offline (see _STORE_WARM_COMMITS in the config).",
                f"git worktree add --detach /tmp/store-warm {warm}",
                "for attempt in 1 2 3; do",
                f"    (cd /tmp/store-warm && pnpm install --frozen-lockfile --ignore-scripts {filters}) && break || true",
                '    echo "prepare: store warm-up attempt $attempt failed; retrying in 15s"',
                "    sleep 15",
                "done",
                "# Non-tolerant re-check: the store now holds that whole dependency set.",
                f"(cd /tmp/store-warm && pnpm install --offline --frozen-lockfile --ignore-scripts {filters})",
                "git worktree remove --force /tmp/store-warm",
                "git worktree prune",
                "",
            ]
        provision += [
            "# turbo's test task depends on setup, which builds every workspace dependency",
            "# (^build: @freecodecamp/shared, curriculum, browser-scripts, ...) first.",
        ]
        for ws in groups:
            provision.append(
                f"pnpm turbo run setup --filter={_WORKSPACES[ws]} --output-logs=errors-only"
            )
        provision += [
            "",
            "# The lockfile these node_modules were installed from (see the run scripts).",
            'sha256sum pnpm-lock.yaml | cut -d" " -f1 > /home/.pnpm-lock.sha256',
            "",
        ]

        gate = [
            "# --- 3. gate ---",
            "# The reporter, the store warm-up, every workspace's vitest and a non-empty",
            "# test list, the built workspace dependencies, and the browser for the",
            "# curriculum suites.",
            "test -s /home/vitest-report.cjs",
            "test -s /home/.pnpm-lock.sha256",
            "test -d packages/shared/dist",
        ]
        if warm:
            gate.append("test -z \"$(git worktree list | sed -n '2,$p')\"")
        for ws in groups:
            gate += [
                f"test -x {ws}/node_modules/.bin/vitest",
                f"(cd {ws} && node_modules/.bin/vitest --version)",
            ]
        if "api" in groups:
            gate.append(
                "(cd api && node --input-type=module -e \"for (const m of ['fastify', "
                "'@prisma/client', 'vitest']) console.log(m + ' -> ' + import.meta.resolve(m))\")"
            )
            # The client prisma generate (api postinstall) wrote; the ungenerated
            # stub throws on construction. Constructing does not connect.
            gate.append(
                "(cd api && node -e \"const { PrismaClient } = require('@prisma/client'); "
                "new PrismaClient(); console.log('prisma client generated')\")"
            )
        if "client" in groups:
            gate.append(
                "(cd client && node --input-type=module -e \"for (const m of ['react', "
                "'react-dom', '@testing-library/react', 'vitest']) console.log(m + ' -> ' + "
                "import.meta.resolve(m))\")"
            )
        if "curriculum" in groups:
            gate += [
                "chromium --version",
                "test -d tools/client-plugins/browser-scripts/dist/js",
                "export PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium",
                "(cd curriculum && ../node_modules/.bin/tsx ./src/test/utils/generate-block-tests.ts)",
            ]
            for f in groups["curriculum"]:
                gate.append(f"test -s curriculum/{f}")
            gate.append(
                "(cd curriculum && node -e \"require.resolve('puppeteer'); "
                "console.log('puppeteer ok')\")"
            )
        else:
            for ws in groups:
                gate += [
                    f"count=$(cd {ws} && node_modules/.bin/vitest list --filesOnly | wc -l)",
                    f'echo "{ws}: vitest list --filesOnly: $count files"',
                    'test "$count" -gt 0',
                ]
        gate.append('echo "DEPS_OK"')

        return (
            f"{_PREPARE_HEADER}"
            "# --- 1. pin ---\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            f'test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            + "\n".join(provision)
            + "\n"
            + "\n".join(gate)
            + "\n"
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_reset_tree()}"
            f"{_test_cmd(self.pr)}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_reset_tree()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_reset_tree()}"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
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

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class FREECODECAMP_70094_TO_67088(Instance):
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

    def dependency(self) -> Optional[Image]:
        return FreeCodeCampImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) (\S.*)$")

        for line in log.splitlines():
            m = result_re.match(line.rstrip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
