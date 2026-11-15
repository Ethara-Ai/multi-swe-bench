import posixpath
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "wheresryan22"
_REPO = "anatomy"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"anatomy_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like anatomy_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# .github/workflows/ci.yml: actions/setup-node with node-version 22.
NODE_IMAGE = "node:22.23.4-bookworm"

# The repo has no package.json. Its CI (ci.yml, "Regression tests") installs these
# exact versions into a throwaway prefix and points NODE_PATH at it; the browser tests
# drive a headless Chrome/Chromium found at /usr/bin/chromium (scripts/drive.mjs).
_TEST_DEPS_DIR = "/opt/anatomy-test-deps"
_TEST_DEPS = "react@19.3.0 react-dom@19.3.0 typescript@5.9.3 esbuild@0.28.2"

_TEST_FILE_RE = re.compile(r"\.test\.(?:js|jsx|mjs|cjs|ts|tsx)$")

_SCRIPT_HEADER = (
    "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n"
    f"export NODE_PATH={_TEST_DEPS_DIR}/node_modules\n\n"
)
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

# The test patches carry PNG fixtures as bare "Binary files differ" stubs, which git
# cannot apply; no test reads them.
_GIT_APPLY = "git apply --whitespace=nowarn --exclude='*.png'"

# Debian's /usr/bin/chromium wrapper sources /etc/chromium.d/*. The container runs as
# root (no user namespace sandbox) with a 64 MB /dev/shm.
_CHROMIUM_FLAGS = (
    'export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --no-sandbox --disable-dev-shm-usage"'
)

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


def _test_files(pr: PullRequest) -> list[str]:
    files = re.findall(r"^diff --git a/\S+ b/(\S+)$", pr.test_patch, re.M)
    return sorted({f for f in files if _TEST_FILE_RE.search(f)})


def _only_new_test_files(pr: PullRequest) -> bool:
    is_new = {}
    for chunk in re.split(r"^(?=diff --git )", pr.test_patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match and _TEST_FILE_RE.search(match.group(1)):
            is_new[match.group(1)] = re.search(r"^new file mode ", chunk, re.M) is not None
    return bool(is_new) and all(is_new.values())


def _test_candidates(pr: PullRequest) -> str:
    files = _test_files(pr)
    if not files or not _only_new_test_files(pr):
        return " ".join(files)
    dirs = " ".join(sorted({posixpath.dirname(f) or "." for f in files}))
    pattern = _TEST_FILE_RE.pattern.replace("?:", "")
    return f"$(find {dirs} -maxdepth 1 -type f | grep -E '{pattern}' | sort)"


def _install_block() -> str:
    return (
        f"mkdir -p {_TEST_DEPS_DIR}\n"
        f"[ -f {_TEST_DEPS_DIR}/package.json ] || "
        f"echo '{{\"name\": \"anatomy-test-deps\", \"private\": true}}' > {_TEST_DEPS_DIR}/package.json\n"
        "for attempt in 1 2 3; do\n"
        f"    npm install --prefix {_TEST_DEPS_DIR} --no-audit --no-fund --ignore-scripts "
        f"{_TEST_DEPS} && break || true\n"
        '    echo "prepare: npm install of the test deps attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    files = _test_candidates(pr)
    return (
        "rm -f /tmp/node-test-results.txt\n"
        "\n"
        "# Only the test files the test patch touches (or, when it only adds new files, the\n"
        "# test files beside them -- ci.yml runs scripts/*.test.mjs); a file the patch adds\n"
        "# does not exist before it is applied, so each stage runs the ones present then.\n"
        "test_files=()\n"
        f"for f in {files}; do\n"
        '    if [ -f "$f" ]; then test_files+=("$f"); fi\n'
        "done\n"
        "\n"
        'if [ "${#test_files[@]}" -gt 0 ]; then\n'
        "    rc=0\n"
        "    # One file at a time: several suites time a real headless chromium page\n"
        "    # (setTimeout/IntersectionObserver windows of a few hundred ms), and eight\n"
        "    # browsers in parallel on a loaded host turn those into flaky failures.\n"
        "    node --test --test-concurrency=1 --test-reporter=/home/node-test-report.mjs "
        '--test-reporter-destination=/tmp/node-test-results.txt "${test_files[@]}" || rc=$?\n'
        "    if [ ! -s /tmp/node-test-results.txt ]; then\n"
        '        echo "node --test exited with code $rc without writing /tmp/node-test-results.txt" >&2\n'
        '        exit "$(( rc == 0 ? 1 : rc ))"\n'
        "    fi\n"
        "    cat /tmp/node-test-results.txt\n"
        "else\n"
        '    echo "no test file of the test patch exists at this stage"\n'
        "fi\n"
    )


class AnatomyImageBase(Image):
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

# Headless browser for the scripts/*.test.mjs suites that drive a page through
# scripts/drive.mjs. Debian's chromium is built for both amd64 and arm64.
RUN apt-get update && apt-get install -y --no-install-recommends \\
    chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/* \\
    && echo '{_CHROMIUM_FLAGS}' > /etc/chromium.d/zz-container \\
    && if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then \\
           test -x /usr/bin/chromium && echo "chromium installed (amd64 under QEMU emulation: the emulated CPU lacks SSE3, so --version cannot run)"; \\
       else chromium --version; fi

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class AnatomyImageDefault(Image):
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
        return AnatomyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
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
            "# Result reporter the run scripts use; written outside the repo.\n"
            "cat > /home/node-test-report.mjs <<'__NODE_TEST_REPORT_EOF__'\n"
            f"{node_test_report}"
            "__NODE_TEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "npm --version\n"
            "\n"
            "# The repo has no package.json; the test-only modules ci.yml installs into a\n"
            "# NODE_PATH prefix, with --ignore-scripts as CI does, outside the repo tree.\n"
            "export npm_config_fund=false npm_config_audit=false npm_config_progress=false\n"
            f"{_install_block()}"
            "\n"
            "# --- 3. gate ---\n"
            "# The reporter, the test modules resolving through NODE_PATH (including esbuild's\n"
            "# native binary for this arch), headless chromium starting, and the node:test\n"
            "# reporter loading under node --test.\n"
            "test -s /home/node-test-report.mjs\n"
            f"NODE_PATH={_TEST_DEPS_DIR}/node_modules node -e \"for (const m of ['react', "
            "'react-dom/server', 'react-dom/client', 'typescript']) console.log(m + ' -> ' + "
            "require.resolve(m)); console.log('esbuild ' + require('esbuild').version + ' -> ' + "
            "require('esbuild').transformSync('let a = 1').code.trim())\"\n"
            "# Chromium refuses to start under QEMU user-mode emulation (the emulated CPU lacks\n"
            "# SSE3), so an amd64 image cross-built on an arm64 host cannot run it at build\n"
            "# time. Detect that case (x86_64 userland over an ARM host CPU) and check the\n"
            "# chromium binary instead; native arm64/amd64 builds run the headless check.\n"
            'if [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    echo "gate: amd64 under QEMU emulation; skipping the headless chromium check"\n'
            "    test -x /usr/bin/chromium\n"
            "else\n"
            "    chromium --version\n"
            "    timeout 120 chromium --headless=new --disable-gpu --dump-dom "
            "'data:text/html,<p id=gate>ok</p>' | grep -q 'id=\"gate\"'\n"
            "fi\n"
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
            "git clean -fd\n"
            f"{_GIT_APPLY} /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fd\n"
            f"{_GIT_APPLY} /home/test.patch /home/fix.patch\n"
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
class ANATOMY_40_TO_31(Instance):
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
        return AnatomyImageDefault(self.pr, self._config)

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
