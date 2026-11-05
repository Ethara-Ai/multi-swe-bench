import posixpath
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "DuarteSantos8"
_REPO = "openGym"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"openGym_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like openGym_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.23.4-bookworm"

_TEST_FILE_RE = re.compile(r"\.test\.(?:js|jsx|mjs|cjs|ts|tsx)$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

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

_NODE_TEST_REPORT_MJS = r"""const ROOT = '/home/__REPO__/';

function rel(file) {
    let f = String(file || '<unknown file>').replace(/\\/g, '/');
    if (f.startsWith('file://')) f = f.slice('file://'.length);
    return f.startsWith(ROOT) ? f.slice(ROOT.length) : f;
}

export default async function* nodeTestReport(source) {
    const pending = new Map();
    const seen = new Map();
    let passed = 0, failed = 0, skipped = 0;

    function* flush(file, node, prefix) {
        const self = node.name === node.rawFile ? '<file failed to run>' : node.name;
        let name = prefix.concat([self]).join(' > ');
        const n = (seen.get(name) || 0) + 1;
        seen.set(name, n);
        if (n > 1) name += ' #' + n;
        if (node.status === 'PASSED') passed++;
        else if (node.status === 'FAILED') failed++;
        else skipped++;
        yield node.status + ' ' + name + '\n';
        for (const child of node.children) yield* flush(file, child, prefix.concat([self]));
    }

    for await (const event of source) {
        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        const data = event.data || {};
        const file = rel(data.file);
        const level = data.nesting || 0;
        if (!pending.has(file)) pending.set(file, []);
        const levels = pending.get(file);
        const children = levels[level + 1] || [];
        levels.length = level + 1;
        let status = event.type === 'test:fail' ? 'FAILED' : 'PASSED';
        if (status === 'PASSED' && (data.skip !== undefined || data.todo !== undefined)) status = 'SKIPPED';
        const node = {name: String(data.name), rawFile: String(data.file || ''), status, children};
        if (level === 0) {
            levels.length = 0;
            yield* flush(file, node, [file]);
        } else {
            (levels[level] = levels[level] || []).push(node);
        }
    }

    yield 'node-test-report: passed=' + passed + ' failed=' + failed + ' skipped=' + skipped + '\n';
}
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


def _test_files(pr: PullRequest, package: str) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    prefix = f"{package}/"
    return sorted(
        {f[len(prefix):] for f in files if f.startswith(prefix) and _TEST_FILE_RE.search(f)}
    )


def _only_new_test_files(pr: PullRequest) -> bool:
    is_new = {}
    for chunk in re.split(r"^(?=diff --git )", pr.test_patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match and _TEST_FILE_RE.search(match.group(1)):
            is_new[match.group(1)] = re.search(r"^new file mode ", chunk, re.M) is not None
    return bool(is_new) and all(is_new.values())


def _test_candidates(pr: PullRequest, package: str) -> str:
    files = _test_files(pr, package)
    if not files or not _only_new_test_files(pr):
        return " ".join(files)
    dirs = " ".join(sorted({posixpath.dirname(f) or "." for f in files}))
    pattern = _TEST_FILE_RE.pattern.replace("?:", "")
    return f"$(cd {package} && find {dirs} -maxdepth 1 -type f | grep -E '{pattern}' | sort)"


def _install_block(package: str, args: str) -> str:
    return (
        "for attempt in 1 2 3; do\n"
        f"    (cd {package} && npm ci {args}) && break || true\n"
        f'    echo "prepare: npm ci in {package}/ attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    fe_files = _test_candidates(pr, "frontend")
    api_files = _test_candidates(pr, "api")
    return (
        "rm -f /tmp/vitest-results.json /tmp/node-test-results.txt\n"
        "\n"
        "# Only the test files the test patch touches; a file the patch adds does not exist\n"
        "# before it is applied, so each stage runs the ones present at that point.\n"
        "fe_files=()\n"
        f"for f in {fe_files}; do\n"
        '    if [ -f "frontend/$f" ]; then fe_files+=("$f"); fi\n'
        "done\n"
        "api_files=()\n"
        f"for f in {api_files}; do\n"
        '    if [ -f "api/$f" ]; then api_files+=("$f"); fi\n'
        "done\n"
        "\n"
        'if [ "${#fe_files[@]}" -gt 0 ]; then\n'
        "    rc=0\n"
        '    (cd frontend && node_modules/.bin/vitest run --reporter=json '
        '--outputFile=/tmp/vitest-results.json "${fe_files[@]}") || rc=$?\n'
        "    node /home/vitest-report.cjs /tmp/vitest-results.json\n"
        "    if [ ! -s /tmp/vitest-results.json ]; then\n"
        '        echo "vitest exited with code $rc without writing /tmp/vitest-results.json" >&2\n'
        '        exit "$(( rc == 0 ? 1 : rc ))"\n'
        "    fi\n"
        "else\n"
        '    echo "no frontend test file of the test patch exists at this stage"\n'
        "fi\n"
        "\n"
        'if [ "${#api_files[@]}" -gt 0 ]; then\n'
        "    rc=0\n"
        "    (cd api && node --test --test-reporter=/home/node-test-report.mjs "
        '--test-reporter-destination=/tmp/node-test-results.txt "${api_files[@]}") || rc=$?\n'
        "    if [ ! -s /tmp/node-test-results.txt ]; then\n"
        '        echo "node --test exited with code $rc without writing /tmp/node-test-results.txt" >&2\n'
        '        exit "$(( rc == 0 ? 1 : rc ))"\n'
        "    fi\n"
        "    cat /tmp/node-test-results.txt\n"
        "else\n"
        '    echo "no api test file of the test patch exists at this stage"\n'
        "fi\n"
    )


class OpenGymImageBase(Image):
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

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class OpenGymImageDefault(Image):
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
        return OpenGymImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        vitest_report = _VITEST_REPORT_CJS.replace("__REPO__", repo)
        node_test_report = _NODE_TEST_REPORT_MJS.replace("__REPO__", repo)

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
            "# Result reporters the run scripts use; written outside the repo.\n"
            "cat > /home/vitest-report.cjs <<'__VITEST_REPORT_EOF__'\n"
            f"{vitest_report}"
            "__VITEST_REPORT_EOF__\n"
            "cat > /home/node-test-report.mjs <<'__NODE_TEST_REPORT_EOF__'\n"
            f"{node_test_report}"
            "__NODE_TEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The same installs CI runs before each suite: frontend with --ignore-scripts\n"
            "# (.gitlab-ci.yml test:frontend), api without the optional Agent SDK (test:api).\n"
            "# The repo root and mcp/ are not installed: no graded test loads them.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            f"{_install_block('frontend', '--ignore-scripts')}"
            f"{_install_block('api', '--omit=optional')}"
            "\n"
            "# --- 3. gate ---\n"
            "# Both reporters, both runners, the modules the frontend tests load (the vite\n"
            "# config's plugin, react-dom/client, the happy-dom and linkedom environments),\n"
            "# the api's runtime deps, vitest finding a non-empty test list, and the\n"
            "# node:test reporter loading under node --test.\n"
            "test -s /home/vitest-report.cjs\n"
            "test -s /home/node-test-report.mjs\n"
            "test -x frontend/node_modules/.bin/vitest\n"
            "(cd frontend && node_modules/.bin/vitest --version)\n"
            "(cd frontend && node --input-type=module -e \"for (const m of ['vite', 'vitest', "
            "'@vitejs/plugin-react', 'react', 'react-dom/client', 'happy-dom', 'linkedom']) "
            "console.log(m + ' -> ' + import.meta.resolve(m))\")\n"
            "fe_count=$(cd frontend && node_modules/.bin/vitest list --filesOnly | wc -l)\n"
            'echo "vitest list --filesOnly: $fe_count files"\n'
            'test "$fe_count" -gt 0\n'
            "(cd api && node --input-type=module -e \"for (const m of ['undici', "
            "'@simplewebauthn/server', 'web-push']) console.log(m + ' -> ' + "
            "import.meta.resolve(m))\")\n"
            "mkdir -p /tmp/node-test-gate\n"
            "printf \"import test from 'node:test'\\ntest('gate', () => {})\\n\" "
            "> /tmp/node-test-gate/gate.test.mjs\n"
            "node --test --test-reporter=/home/node-test-report.mjs "
            "--test-reporter-destination=/tmp/node-test-gate/out.txt "
            "/tmp/node-test-gate/gate.test.mjs\n"
            "cat /tmp/node-test-gate/out.txt\n"
            "grep -q '^PASSED .*gate$' /tmp/node-test-gate/out.txt\n"
            "rm -rf /tmp/node-test-gate\n"
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
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
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
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class OPENGYM_570_TO_194(Instance):
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
        return OpenGymImageDefault(self.pr, self._config)

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
