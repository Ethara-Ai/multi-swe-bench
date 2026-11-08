import json
import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "NomaDamas"
_REPO = "k-skill"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"k_skill_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like k_skill_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

NODE_IMAGE = "node:22.23.4-trixie"

_VENV_PYTHON = ".cache/python-test-venv/bin/python"
_FIX_DEPS_TAR = "/home/mswe-fix-node-modules.tar.gz"
_TEST_CMD = "node /home/mswe-run-tests.cjs\n"
_NOT_A_TEST = re.compile(r"^[^ ]+\.(?:sh|js|cjs|mjs)$")

_ENV_EXPORTS = "export CI=true\nexport NODE_OPTIONS=--max-old-space-size=4096\n\n"
_SCRIPT_HEADER = f"#!/bin/bash\nset -eo pipefail\n\n{_ENV_EXPORTS}"
_PREPARE_HEADER = f"#!/bin/bash\nset -e\n\n{_ENV_EXPORTS}"

_NODE_REPORTER_MJS = r"""import fs from 'node:fs';

const ROOT = '/home/__REPO__/';
const OUT = process.env.MSWE_RESULTS;
const clean = (s) => String(s).split(ROOT).join('').replace(/\s+/g, ' ').trim();

export default async function* msweReporter(source) {
	const stacks = new Map();
	const seen = new Map();
	for await (const event of source) {
		const {type, data} = event;
		if (type !== 'test:start' && type !== 'test:pass' && type !== 'test:fail') continue;
		const file = data.file ? clean(data.file) : '<none>';
		const stack = stacks.get(file) || [];
		stacks.set(file, stack);
		if (type === 'test:start') {
			stack[data.nesting] = clean(data.name);
			continue;
		}
		if (type === 'test:pass' && data.details && data.details.type === 'suite') continue;
		const leaf = clean(data.name);
		const parts = [...stack.slice(0, data.nesting), leaf];
		if (parts[0] !== file) parts.unshift(file);
		let name = parts.join(' > ');
		const n = (seen.get(name) || 0) + 1;
		seen.set(name, n);
		if (n > 1) name += ' #' + n;
		let status = type === 'test:pass' ? 'PASSED' : 'FAILED';
		if (data.skip !== undefined || data.todo !== undefined) status = 'SKIPPED';
		fs.appendFileSync(OUT, 'MSWE_RESULT ' + status + ' :: ' + name + '\n');
	}
}
"""

_PYTHON_RESULTS_PY = r"""import os
import sys
import unittest.result as _result

_OUT = os.environ.get("MSWE_RESULTS")
_ROOT = "/home/__REPO__/"
_open, _write, _close = os.open, os.write, os.close
_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT


def _emit(status, test):
    module = sys.modules.get(type(test).__module__)
    path = getattr(module, "__file__", None) or ""
    name = " ".join(test.id().split())
    if path.startswith(_ROOT):
        name = path[len(_ROOT):] + " > " + name
    fd = _open(_OUT, _FLAGS, 0o644)
    try:
        _write(fd, ("MSWE_RESULT " + status + " :: " + name + "\n").encode("utf-8", "replace"))
    finally:
        _close(fd)


def _wrap(method, status):
    original = getattr(_result.TestResult, method)

    def wrapper(self, test, *args):
        _emit(status, test)
        return original(self, test, *args)

    setattr(_result.TestResult, method, wrapper)


if _OUT:
    _wrap("addSuccess", "PASSED")
    _wrap("addFailure", "FAILED")
    _wrap("addError", "FAILED")
    _wrap("addSkip", "SKIPPED")
    _wrap("addExpectedFailure", "PASSED")
    _wrap("addUnexpectedSuccess", "FAILED")
    _add_subtest = _result.TestResult.addSubTest

    def _sub_test(self, test, subtest, err):
        if err is not None:
            _emit("FAILED", test)
        return _add_subtest(self, test, subtest, err)

    _result.TestResult.addSubTest = _sub_test
"""

_KEEP_GOING_CJS = r"""const cp = require('node:child_process');
const fs = require('node:fs');

const original = cp.spawnSync;
cp.spawnSync = (command, args, options) => {
	const list = args || [];
	if (list.includes('prepare:python-test-env')) return {status: 0, signal: null, output: []};
	const result = original(command, args, options);
	if (list.some((a) => String(a).endsWith('validate-skills.sh'))) {
		fs.appendFileSync(
			process.env.MSWE_RESULTS,
			'MSWE_RESULT ' + (result.status === 0 ? 'PASSED' : 'FAILED') + ' :: scripts/validate-skills.sh\n',
		);
	}
	if (list.includes('--version')) return result;
	return {...result, status: 0, error: undefined};
};
"""

_RUN_TESTS_CJS = r"""const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = '/home/__REPO__';
const OUT = '/tmp/mswe-results.txt';
const PYTHON = './__VENV_PYTHON__';
const EXTRA_TESTS = __EXTRA_TESTS__;

process.chdir(ROOT);
fs.rmSync(OUT, {force: true});
const env = {
	...process.env,
	MSWE_RESULTS: OUT,
	NODE_OPTIONS: ((process.env.NODE_OPTIONS || '') + ' --test-reporter=/home/mswe-node-reporter.mjs').trim(),
};

const bash = (command, extra = {}) => spawnSync('bash', ['-c', command], {stdio: 'inherit', env: {...env, ...extra}});
const results = () => (fs.existsSync(OUT) ? fs.readFileSync(OUT, 'utf8') : '');

const testScript = JSON.parse(fs.readFileSync('package.json', 'utf8')).scripts.test;
if (testScript.includes('run-tests.js')) {
	const run = spawnSync(process.execPath, ['-r', '/home/mswe-keep-going.cjs', 'scripts/run-tests.js'], {stdio: 'inherit', env});
	console.log('MSWE_LOG run-tests.js exited with ' + run.status);
} else {
	for (const segment of testScript.split(' && ')) {
		if (segment.includes('prepare:python-test-env')) continue;
		const run = bash(segment);
		console.log('MSWE_LOG exit ' + run.status + ': ' + segment.slice(0, 80));
		if (segment.includes('validate-skills.sh')) {
			const status = run.status === 0 ? 'PASSED' : 'FAILED';
			fs.appendFileSync(OUT, 'MSWE_RESULT ' + status + ' :: scripts/validate-skills.sh\n');
		}
	}
}

for (const file of EXTRA_TESTS) {
	if (!fs.existsSync(file) || results().includes(' :: ' + file + ' > ')) continue;
	console.log('MSWE_LOG test file not reached by the test command: ' + file);
	if (file.endsWith('.py')) {
		const dir = path.dirname(file);
		const skill = file.split('/')[0];
		const pythonPath = ['.', 'scripts', skill + '/scripts', skill + '/tests'].join(':');
		bash(`${PYTHON} -m unittest discover -s '${dir}' -p '${path.basename(file)}'`, {PYTHONPATH: pythonPath, PYTHONNOUSERSITE: '1'});
	} else {
		bash(`node --test '${file}'`);
	}
}

const output = results();
if (!output.trim()) {
	console.error('no test results were written');
	process.exit(1);
}
process.stdout.write('\n' + output);
"""

_CHECK_DEPS_CJS = r"""const fs = require('node:fs');

const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
const missing = [];
let checked = 0;
for (const [key, entry] of Object.entries(lock.packages || {})) {
	if (entry.link) {
		if (!fs.existsSync(key)) missing.push(key);
		continue;
	}
	if (!key.includes('node_modules/') || entry.optional) continue;
	checked++;
	if (!fs.existsSync(key + '/package.json')) missing.push(key);
}
console.log('locked packages checked: ' + checked + ', missing: ' + missing.length);
if (missing.length || checked === 0) {
	console.error(missing.slice(0, 10).join('\n'));
	process.exit(1);
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


def _patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/\S+ b/(\S+)$", patch, re.M)


def _patch_section(patch: str, path: str) -> str:
    match = re.search(
        rf"^diff --git a/\S+ b/{re.escape(path)}\n(.*?)(?=^diff --git |\Z)", patch, re.M | re.S
    )
    return match.group(1) if match else ""


def _all_patch_files(pr: PullRequest) -> list[str]:
    return _patch_files(pr.test_patch) + _patch_files(pr.fix_patch)


def _nested_lock_dirs(pr: PullRequest) -> list[str]:
    return sorted(
        {
            f.rsplit("/", 1)[0]
            for f in _all_patch_files(pr)
            if f.endswith("/package-lock.json") and not f.startswith("packages/")
        }
    )


def _changes_node_modules(pr: PullRequest) -> bool:
    return any(
        f == "package-lock.json"
        or re.fullmatch(r"packages/[^/]+/package\.json", f)
        or f.endswith("/package-lock.json")
        for f in _all_patch_files(pr)
    )


def _changes_python_env(pr: PullRequest) -> bool:
    if "scripts/prepare-python-test-env.js" in _all_patch_files(pr):
        return True
    root_package = _patch_section(pr.fix_patch, "package.json")
    return bool(re.search(r"^\+.*prepare:python-test-env", root_package, re.M))


def _extra_tests(pr: PullRequest) -> list[str]:
    tests = []
    for f in _patch_files(pr.test_patch):
        if "/fixtures/" in f or f.startswith("packages/k-skill-cli/skills/"):
            continue
        name = f.rsplit("/", 1)[-1]
        is_node = name.endswith(".test.js") or re.fullmatch(r"test_.*\.js", name)
        is_python = re.fullmatch(r"test_.*\.py", name)
        if is_node or is_python:
            tests.append(f)
    return tests


def _write_helpers_block(pr: PullRequest) -> str:
    run_tests = (
        _RUN_TESTS_CJS.replace("__REPO__", pr.repo)
        .replace("__VENV_PYTHON__", _VENV_PYTHON)
        .replace("__EXTRA_TESTS__", json.dumps(_extra_tests(pr)))
    )
    return (
        "write_helpers() {\n"
        "cat > /home/mswe-node-reporter.mjs <<'__MSWE_NODE_REPORTER_EOF__'\n"
        f"{_NODE_REPORTER_MJS.replace('__REPO__', pr.repo)}"
        "__MSWE_NODE_REPORTER_EOF__\n"
        "cat > /home/mswe_unittest_results.py <<'__MSWE_PYTHON_RESULTS_EOF__'\n"
        f"{_PYTHON_RESULTS_PY.replace('__REPO__', pr.repo)}"
        "__MSWE_PYTHON_RESULTS_EOF__\n"
        "cat > /home/mswe-keep-going.cjs <<'__MSWE_KEEP_GOING_EOF__'\n"
        f"{_KEEP_GOING_CJS}"
        "__MSWE_KEEP_GOING_EOF__\n"
        "cat > /home/mswe-run-tests.cjs <<'__MSWE_RUN_TESTS_EOF__'\n"
        f"{run_tests}"
        "__MSWE_RUN_TESTS_EOF__\n"
        "cat > /home/mswe-check-deps.cjs <<'__MSWE_CHECK_DEPS_EOF__'\n"
        f"{_CHECK_DEPS_CJS}"
        "__MSWE_CHECK_DEPS_EOF__\n"
        "}\n"
        "write_helpers\n"
        "\n"
    )


def _install_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    npm ci --no-audit --no-fund && break || true\n"
        '    echo "prepare: npm ci attempt $attempt failed; trying npm install in 15s"\n'
        "    sleep 15\n"
        "    npm install --no-audit --no-fund && break || true\n"
        "done\n"
        "test -d node_modules/typescript\n"
    )


def _prepare_python_block() -> str:
    return (
        "for attempt in 1 2 3; do\n"
        "    npm run prepare:python-test-env && break || true\n"
        '    echo "prepare: python test env attempt $attempt failed; retrying in 15s"\n'
        "    sleep 15\n"
        "done\n"
        f"{_VENV_PYTHON} -c 'import bs4'\n"
    )


def _fix_deps_block(pr: PullRequest) -> str:
    if not (_changes_node_modules(pr) or _changes_python_env(pr)):
        return ""
    block = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
    if _changes_python_env(pr):
        block += _prepare_python_block()
    if _changes_node_modules(pr):
        block += _install_block()
        for lock_dir in _nested_lock_dirs(pr):
            block += f"(cd {lock_dir} && npm ci --no-audit --no-fund)\n"
        block += (
            "node /home/mswe-check-deps.cjs\n"
            "find . -type d -name node_modules -prune -print0 "
            f"| tar --null -T - --use-compress-program='gzip -1' -cf {_FIX_DEPS_TAR}\n"
            f"test -s {_FIX_DEPS_TAR}\n"
        )
    block += "git reset --hard\ngit clean -fdx -e .cache\n\n"
    return block


class KSkillImageBase(Image):
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

RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class KSkillImageDefault(Image):
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
        return KSkillImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        return (
            f"{_PREPARE_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout --detach {self.pr.base.sha}\n"
            f'test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            f"{_write_helpers_block(self.pr)}"
            "node --version\n"
            "npm --version\n"
            "\n"
            f"{_fix_deps_block(self.pr)}"
            f"{_install_block()}"
            f"{_prepare_python_block()}"
            f'SITE_PACKAGES=$({_VENV_PYTHON} -c "import site; print(site.getsitepackages()[0])")\n'
            'cp /home/mswe_unittest_results.py "$SITE_PACKAGES/"\n'
            'echo "import mswe_unittest_results" > "$SITE_PACKAGES/mswe_unittest_results.pth"\n'
            "git reset --hard\n"
            "git clean -fd\n"
            "\n"
            "test -s /home/mswe-node-reporter.mjs\n"
            "test -s /home/mswe-keep-going.cjs\n"
            "test -s /home/mswe-run-tests.cjs\n"
            "test -d node_modules/typescript\n"
            "node /home/mswe-check-deps.cjs\n"
            f"MSWE_RESULTS=/dev/null {_VENV_PYTHON} -c "
            "\"import unittest.result as r; assert r.TestResult.addSuccess.__module__ == 'mswe_unittest_results'\"\n"
            "test_count=$(git ls-files | grep -cE '(\\.test\\.js|/test_[^/]+\\.(js|py))$')\n"
            'echo "test files: $test_count"\n'
            'test "$test_count" -gt 0\n'
            'echo "DEPS_OK"\n'
        )

    def _run_sh(self) -> str:
        return f"{_SCRIPT_HEADER}cd /home/{self.pr.repo}\n{_TEST_CMD}"

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_TEST_CMD}"
        )

    def _fix_run_sh(self) -> str:
        restore_deps = (
            "find . -type d -name node_modules -prune -exec rm -rf {} +\n"
            f"tar -xzf {_FIX_DEPS_TAR}\n"
            if _changes_node_modules(self.pr)
            else ""
        )
        return (
            f"{_SCRIPT_HEADER}"
            f"cd /home/{self.pr.repo}\n"
            "git reset --hard\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            f"{restore_deps}"
            f"{_TEST_CMD}"
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
class K_SKILL_725_TO_434(Instance):
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
        return KSkillImageDefault(self.pr, self._config)

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
            if _NOT_A_TEST.match(name):
                continue
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
