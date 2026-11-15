import posixpath
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "justlovemaki"
_REPO = "AIClient2API"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"aiclient2api_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like aiclient2api_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# The repo's Dockerfile ships node:20-alpine (undici needs >=20.18.1); Node 20 reached
# end of life before this era's PRs (Oct 2026), so the maintained 22 LTS line is used.
# Every engines constraint in package-lock.json (undici >=20.18.1, babel-jest 30:
# ^22.0.0, @vercel/oidc >=20) accepts it.
NODE_IMAGE = "node:22.23.4-bookworm"

# package-lock.json is stale at this era (it lacks @github/copilot-sdk 1.0.16 and koffi,
# so `npm ci` refuses it); pnpm-lock.yaml (lockfileVersion 9.0, carrying the
# package.json "pnpm.overrides") is the lockfile kept in sync with package.json.
# No packageManager field pins pnpm: 10.34.6 is the pnpm 10 release current when the
# era's base commit was made (released 2026-09-28; base commit 2026-10-09).
PNPM_VERSION = "10.34.6"

_TEST_FILE_RE = re.compile(r"\.test\.js$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

_JEST_REPORT_CJS = r"""const fs = require('fs');

const path = process.argv[2];
const ROOT = '/home/__REPO__/';

if (!path || !fs.existsSync(path)) {
    console.log('jest-report: no results file at ' + path + ' -- jest produced no output');
    process.exit(0);
}

let report;
try {
    report = JSON.parse(fs.readFileSync(path, 'utf8'));
} catch (e) {
    console.log('jest-report: could not parse ' + path + ': ' + e.message);
    process.exit(0);
}

const STATUS = {passed: 'PASSED', failed: 'FAILED', pending: 'SKIPPED', skipped: 'SKIPPED', todo: 'SKIPPED', disabled: 'SKIPPED', focused: 'PASSED'};

const suites = (report.testResults || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
for (const suite of suites) {
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

console.log('jest-report: suites=' + (report.numTotalTestSuites || 0) +
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


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if f.startswith("tests/") and _TEST_FILE_RE.search(f)})


def _only_new_test_files(pr: PullRequest) -> bool:
    is_new = {}
    for chunk in re.split(r"^(?=diff --git )", pr.test_patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match and _TEST_FILE_RE.search(match.group(1)):
            is_new[match.group(1)] = re.search(r"^new file mode ", chunk, re.M) is not None
    return bool(is_new) and all(is_new.values())


def _test_candidates(pr: PullRequest) -> str:
    # The test files the test patch touches. When the patch only ADDS test files,
    # none exists at the baseline, so the directories holding them are run whole
    # (the baseline stage then still observes the suite next to the new files).
    files = _test_files(pr)
    if not files or not _only_new_test_files(pr):
        return " ".join(files)
    # tests/api-integration.test.js is left out of that sweep: it drives a separately
    # started server at http://localhost:3000 (see its header) and fails identically
    # in every stage inside the container.
    dirs = " ".join(sorted({posixpath.dirname(f) for f in files}))
    return (
        f"$(find {dirs} -maxdepth 1 -type f -name '*.test.js' "
        "! -name 'api-integration.test.js' | sort)"
    )


def _install_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    pnpm install --frozen-lockfile && break || true\n"
        '    echo "prepare: pnpm install attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    return (
        "rm -f /tmp/jest-results.json\n"
        "\n"
        "# Only the test files the test patch touches; a file the patch adds does not exist\n"
        "# before it is applied, so each stage runs the ones present at that point.\n"
        "files=()\n"
        f"for f in {_test_candidates(pr)}; do\n"
        '    if [ -f "$f" ]; then files+=("$f"); fi\n'
        "done\n"
        'if [ "${#files[@]}" -eq 0 ]; then\n'
        '    echo "no test file of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "rc=0\n"
        "node_modules/.bin/jest --ci --runInBand --json --outputFile=/tmp/jest-results.json "
        '--runTestsByPath "${files[@]}" || rc=$?\n'
        "node /home/jest-report.cjs /tmp/jest-results.json\n"
        "if [ ! -s /tmp/jest-results.json ]; then\n"
        '    echo "jest exited with code $rc without writing /tmp/jest-results.json" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
    )


class Aiclient2apiImageBase(Image):
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

RUN npm install -g pnpm@{PNPM_VERSION} && pnpm --version

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Aiclient2apiImageDefault(Image):
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
        return Aiclient2apiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        jest_report = _JEST_REPORT_CJS.replace("__REPO__", repo)

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
            "# --- 2. provision ---\n"
            "# Result reporter the run scripts use; written outside the repo.\n"
            "cat > /home/jest-report.cjs <<'__JEST_REPORT_EOF__'\n"
            f"{jest_report}"
            "__JEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "pnpm --version\n"
            "\n"
            "# Frozen install from the pnpm-lock.yaml that ships with this commit (see\n"
            "# PNPM_VERSION above for why not package-lock.json).\n"
            f"{_install_block()}"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, jest and the babel transform jest.config.js/.babelrc configure,\n"
            "# runtime deps the provider/converter modules under test import, jest finding\n"
            "# a non-empty test list, and the reporter turning jest JSON into result lines.\n"
            "test -s /home/jest-report.cjs\n"
            "test -x node_modules/.bin/jest\n"
            "node_modules/.bin/jest --version\n"
            "node -e \"for (const m of ['babel-jest', '@babel/preset-env', "
            "'babel-plugin-transform-import-meta', '@jest/globals', 'supertest', 'axios', "
            "'undici', 'uuid', 'google-auth-library']) console.log(m + ' -> ' + "
            "require.resolve(m))\"\n"
            "count=$(node_modules/.bin/jest --listTests | wc -l)\n"
            'echo "jest --listTests: $count files"\n'
            'test "$count" -gt 0\n'
            "mkdir -p /tmp/jest-gate\n"
            "printf '%s' '{\"numTotalTestSuites\":1,\"numTotalTests\":1,\"numPassedTests\":1,"
            "\"testResults\":[{\"name\":\"/home/"
            f"{repo}"
            "/gate.test.js\",\"status\":\"passed\",\"assertionResults\":[{\"ancestorTitles\":"
            "[\"g\"],\"title\":\"gate\",\"status\":\"passed\"}]}]}' > /tmp/jest-gate/r.json\n"
            "node /home/jest-report.cjs /tmp/jest-gate/r.json | tee /tmp/jest-gate/out.txt\n"
            "grep -qx 'PASSED gate.test.js > g > gate' /tmp/jest-gate/out.txt\n"
            "rm -rf /tmp/jest-gate\n"
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            f"{_test_cmd(self.pr)}"
        )

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
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


_LOAD_FAILURE_MARKERS = frozenset(
    {"<suite failed to run>", "<file failed to load>", "<file failed to run>"}
)


def _add_file_results(passed: set[str], failed: set[str], skipped: set[str]) -> None:
    """Add one result per test file, named by the bare file path.

    A test file that cannot load before the fix (it imports code the fix adds) is
    reported under a placeholder such as `<file> > <suite failed to run>`, while after
    the fix its tests pass under their own names, so no single name links the stages.
    The file-level result does: FAILED when the file failed to load, when any of its
    tests failed, or when it ran no test at all (node:test then reports one passing
    entry named after the file itself); PASSED when its tests passed; SKIPPED when
    every test in it was skipped.
    """
    files: dict[str, dict] = {}
    for status, names in (("passed", passed), ("failed", failed), ("skipped", skipped)):
        for name in list(names):
            file, sep, rest = name.partition(" > ")
            if not sep or file.startswith("<"):
                continue
            st = files.setdefault(
                file,
                {"passed": 0, "failed": 0, "skipped": 0, "load_failed": False, "empty": False},
            )
            if rest in _LOAD_FAILURE_MARKERS:
                st["load_failed"] = True
            elif rest == file and status == "passed":
                st["empty"] = True
                names.discard(name)
            else:
                st[status] += 1
    for file, st in files.items():
        if st["load_failed"] or st["failed"] or (st["empty"] and not st["passed"]):
            failed.add(file)
        elif st["passed"]:
            passed.add(file)
        elif st["skipped"]:
            skipped.add(file)


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class AICLIENT2API_732_TO_718(Instance):
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
        return Aiclient2apiImageDefault(self.pr, self._config)

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

        _add_file_results(passed_tests, failed_tests, skipped_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
