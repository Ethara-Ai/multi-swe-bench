import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "Ripple-TS"
_REPO = "ripple"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"ripple_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like ripple_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.23.4-trixie"

_FIX_DEPS_TAR = "/home/mswe-fix-node-modules.tar.gz"

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n\n"
_SCRIPT_HEADER = f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}"
_PREPARE_HEADER = f"#!/bin/bash\nset -e\n\n{_ENV_EXPORTS}"

_VITEST_REPORTER_MJS = r"""import fs from 'node:fs';

const ROOT = '/home/__REPO__/';
const OUT = process.env.MSWE_RESULTS;
const clean = (s) => String(s).replace(/\s+/g, ' ').trim();
const rel = (f) => (f.startsWith(ROOT) ? f.slice(ROOT.length) : f);

export default class MsweReporter {
	onTestRunEnd(testModules, unhandledErrors, reason) {
		const out = [];
		const counts = {PASSED: 0, FAILED: 0, SKIPPED: 0};
		const seen = new Map();
		const emit = (status, name) => {
			const n = (seen.get(name) || 0) + 1;
			seen.set(name, n);
			counts[status]++;
			out.push('MSWE_RESULT ' + status + ' :: ' + (n > 1 ? name + ' #' + n : name));
		};
		const modules = [...testModules].map((m) => ({
			m,
			prefix: (m.project.name || '<root>') + ' > ' + rel(m.moduleId),
		}));
		modules.sort((a, b) => (a.prefix < b.prefix ? -1 : a.prefix > b.prefix ? 1 : 0));
		for (const {m, prefix} of modules) {
			const tests = [...m.children.allTests()];
			if (m.state() === 'failed' && !tests.some((t) => t.result().state === 'failed')) {
				emit('FAILED', prefix + ' > <suite failed to run>');
				for (const e of m.errors()) out.push('MSWE_LOG ' + clean(e.message).slice(0, 300));
			}
			for (const t of tests) {
				const r = t.result();
				const status = r.state === 'passed' ? 'PASSED' : r.state === 'failed' ? 'FAILED' : 'SKIPPED';
				emit(status, prefix + ' > ' + clean(t.fullName));
				if (status === 'FAILED') {
					for (const e of r.errors || []) out.push('MSWE_LOG ' + clean(e.message).slice(0, 300));
				}
			}
		}
		out.push(
			'MSWE_SUMMARY vitest reason=' + reason + ' modules=' + modules.length +
				' passed=' + counts.PASSED + ' failed=' + counts.FAILED +
				' skipped=' + counts.SKIPPED + ' unhandled=' + unhandledErrors.length,
		);
		fs.writeFileSync(OUT, out.join('\n') + '\n');
	}
}
"""

_TREE_SITTER_REPORT_CJS = r"""const fs = require('fs');

const log = fs.readFileSync(process.argv[2], 'utf8').replace(/\x1b\[[0-9;]*[a-zA-Z]/g, '');
const counts = {PASSED: 0, FAILED: 0, SKIPPED: 0};
const seen = new Map();
let corpus = '<root>';

for (const line of log.split('\n')) {
	const head = /^\s+([^\s:][^:]*):\s*$/.exec(line);
	if (head) {
		corpus = head[1];
		continue;
	}
	const m = /^\s*\d+\.\s+(✓|✗|✔|✘|⌀)\s+(.+)$/.exec(line);
	if (!m) continue;
	const status = m[1] === '✓' || m[1] === '✔' ? 'PASSED' : m[1] === '⌀' ? 'SKIPPED' : 'FAILED';
	let name = 'tree-sitter > ' + corpus + ' > ' + m[2].replace(/\s+-- Warning:.*$/, '').trim();
	const n = (seen.get(name) || 0) + 1;
	seen.set(name, n);
	if (n > 1) name += ' #' + n;
	counts[status]++;
	console.log('MSWE_RESULT ' + status + ' :: ' + name);
}

console.log('MSWE_SUMMARY tree-sitter passed=' + counts.PASSED + ' failed=' + counts.FAILED + ' skipped=' + counts.SKIPPED);
if (counts.PASSED + counts.FAILED + counts.SKIPPED === 0) {
	console.log(log);
	process.exit(1);
}
"""

_BUILD_SH = """#!/bin/bash
set -eo pipefail

cd /home/__REPO__

export npm_config_verify_deps_before_run=false pnpm_config_verify_deps_before_run=false

for pkg in typescript-plugin language-server create-ripple cli; do
    if [ -x "packages/$pkg/node_modules/.bin/tsdown" ]; then
        (cd "packages/$pkg" && node_modules/.bin/tsdown)
    fi
done

if [ -f packages/eslint-parser/package.json ]; then
    (cd packages/eslint-parser && pnpm build)
fi
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


def _patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch, re.M)


def _changes_install(pr: PullRequest) -> bool:
    files = _patch_files(pr.test_patch) + _patch_files(pr.fix_patch)
    return "pnpm-lock.yaml" in files or "pnpm-workspace.yaml" in files


def _runs_tree_sitter(pr: PullRequest) -> bool:
    return any(f.startswith("grammars/tree-sitter/") for f in _patch_files(pr.test_patch))


def _test_patch_breaks_hydration_setup(pr: PullRequest) -> bool:
    sections = re.findall(
        r"^diff --git a/\S+ b/(\S+)\n(.*?)(?=^diff --git |\Z)", pr.test_patch, re.M | re.S
    )
    return any(
        re.search(r"(^|/)tests/hydration/components/.*\.tsrx$", name)
        and re.search(r"^-(?!--).*&\[", body, re.M)
        for name, body in sections
    )


def _write_helpers_block(pr: PullRequest) -> str:
    tree_sitter_report = (
        "cat > /home/mswe-tree-sitter-report.cjs <<'__MSWE_TREE_SITTER_REPORT_EOF__'\n"
        f"{_TREE_SITTER_REPORT_CJS}"
        "__MSWE_TREE_SITTER_REPORT_EOF__\n"
        if _runs_tree_sitter(pr)
        else ""
    )
    return (
        "write_helpers() {\n"
        "cat > /home/mswe-build.sh <<'__MSWE_BUILD_EOF__'\n"
        f"{_BUILD_SH.replace('__REPO__', pr.repo)}"
        "__MSWE_BUILD_EOF__\n"
        "cat > /home/mswe-vitest-reporter.mjs <<'__MSWE_VITEST_REPORTER_EOF__'\n"
        f"{_VITEST_REPORTER_MJS.replace('__REPO__', pr.repo)}"
        "__MSWE_VITEST_REPORTER_EOF__\n"
        f"{tree_sitter_report}"
        "}\n"
        "write_helpers\n"
        "\n"
    )


def _install_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    pnpm install --prod false --frozen-lockfile && break || true\n"
        '    echo "prepare: pnpm install attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
    )


def _fix_deps_block() -> str:
    return (
        "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        f"{_install_block()}"
        "test -x node_modules/.bin/vitest\n"
        "find . -type d -name node_modules -prune -print0 "
        f"| tar --null -T - --use-compress-program='gzip -1' -cf {_FIX_DEPS_TAR}\n"
        f"test -s {_FIX_DEPS_TAR}\n"
        "git reset --hard\n"
        "git clean -fdx\n"
        "\n"
    )


def _hydration_setup_fallback(pr: PullRequest) -> str:
    if not _test_patch_breaks_hydration_setup(pr):
        return ""
    return (
        "if grep -q '^MSWE_SUMMARY vitest reason=failed modules=0 ' /tmp/mswe-vitest.txt; then\n"
        "    rm -f /tmp/mswe-vitest.txt\n"
        "    rc=0\n"
        "    MSWE_RESULTS=/tmp/mswe-vitest.txt node_modules/.bin/vitest run --maxWorkers=4 "
        "--project '!ripple-hydration' --reporter=/home/mswe-vitest-reporter.mjs || rc=$?\n"
        "    if [ ! -s /tmp/mswe-vitest.txt ]; then\n"
        '        echo "vitest exited with code $rc without writing /tmp/mswe-vitest.txt" >&2\n'
        '        exit "$(( rc == 0 ? 1 : rc ))"\n'
        "    fi\n"
        "fi\n"
    )


def _test_cmd(pr: PullRequest) -> str:
    cmd = (
        "rm -f /tmp/mswe-vitest.txt\n"
        "rc=0\n"
        "MSWE_RESULTS=/tmp/mswe-vitest.txt node_modules/.bin/vitest run --maxWorkers=4 "
        "--reporter=/home/mswe-vitest-reporter.mjs || rc=$?\n"
        "if [ ! -s /tmp/mswe-vitest.txt ]; then\n"
        '    echo "vitest exited with code $rc without writing /tmp/mswe-vitest.txt" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "fi\n"
        f"{_hydration_setup_fallback(pr)}"
        "cat /tmp/mswe-vitest.txt\n"
    )
    if _runs_tree_sitter(pr):
        cmd += (
            "\n"
            "rc=0\n"
            "(cd grammars/tree-sitter && node_modules/.bin/tree-sitter test --overview-only) "
            "> /tmp/mswe-tree-sitter.txt 2>&1 || rc=$?\n"
            'echo "tree-sitter test exited with code $rc"\n'
            "node /home/mswe-tree-sitter-report.cjs /tmp/mswe-tree-sitter.txt\n"
        )
    return cmd


class RippleImageBase(Image):
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


class RippleImageDefault(Image):
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
        return RippleImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        fix_deps = _fix_deps_block() if _changes_install(self.pr) else ""
        tree_sitter_gate = (
            "(cd grammars/tree-sitter && node_modules/.bin/tree-sitter --version)\n"
            if _runs_tree_sitter(self.pr)
            else ""
        )

        return (
            f"{_PREPARE_HEADER}"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            f'test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            f"{_write_helpers_block(self.pr)}"
            "PNPM_VERSION=$(node -p \"require('./package.json').packageManager.split('@')[1]\")\n"
            'npm install -g "pnpm@$PNPM_VERSION"\n'
            "node --version\n"
            "pnpm --version\n"
            "\n"
            f"{fix_deps}"
            f"{_install_block()}"
            "bash /home/mswe-build.sh\n"
            "git reset --hard\n"
            "git clean -fd\n"
            "\n"
            "test -s /home/mswe-vitest-reporter.mjs\n"
            "test -x node_modules/.bin/vitest\n"
            "node_modules/.bin/vitest --version\n"
            f"{tree_sitter_gate}"
            "test_count=$(node_modules/.bin/vitest list --filesOnly | wc -l)\n"
            'echo "vitest list --filesOnly: $test_count files"\n'
            'test "$test_count" -gt 0\n'
            "node --input-type=module -e \"for (const m of ['vitest', 'jsdom', 'linkedom', "
            "'@ripple-ts/vite-plugin']) console.log(m + ' -> ' + import.meta.resolve(m))\"\n"
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
        restore_deps = (
            "find . -type d -name node_modules -prune -exec rm -rf {} +\n"
            f"tar -xzf {_FIX_DEPS_TAR}\n"
            if _changes_install(self.pr)
            else ""
        )
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{restore_deps}"
            "bash /home/mswe-build.sh\n"
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
class RIPPLE_1513_TO_1260(Instance):
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
        return RippleImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^MSWE_RESULT (PASSED|FAILED|SKIPPED) :: (\S.*)$")

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
