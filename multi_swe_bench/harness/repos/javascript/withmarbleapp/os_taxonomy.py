import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24.21.0-bookworm"
_BASE_TAG = "base"
_APT_PACKAGES = ["git", "ca-certificates", "coreutils"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


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


_EMIT_TESTCASES_MJS = """import path from 'node:path';

const repoDir = process.cwd();
const clean = (v) => String(v === undefined || v === null ? '' : v).replace(/\\s+/g, ' ').trim();

export default async function* reporter(source) {
    const stacks = new Map();
    const seen = new Map();

    for await (const event of source) {
        const data = event.data || {};
        const file = data.file || process.env.MSB_TEST_FILE || '';

        if (event.type === 'test:start') {
            if (!stacks.has(file)) stacks.set(file, []);
            const stack = stacks.get(file);
            stack.length = data.nesting;
            stack[data.nesting] = data.name;
            continue;
        }

        if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
        if ((data.details || {}).type === 'suite') continue;

        const rel = (path.relative(repoDir, file) || file).split(path.sep).join('/');
        const parts = [rel, ...(stacks.get(file) || []).slice(0, data.nesting).map(clean)].filter(Boolean);

        const title = clean(data.name);
        if (title && title !== rel && title !== file) parts.push(title);

        let name = parts.join(' > ');
        const count = (seen.get(name) || 0) + 1;
        seen.set(name, count);
        if (count > 1) name += ' #' + count;

        const status = event.type === 'test:fail' ? 'FAILED'
            : data.skip || data.todo ? 'SKIPPED' : 'PASSED';
        yield '\\nTESTCASE ' + status + ' ' + name + '\\n';
        if (status === 'FAILED') {
            const error = (data.details || {}).error || {};
            yield '    error: ' + clean((error.cause || {}).message || error.message) + '\\n';
        }
    }
}
"""


_RUN_ENV = """export CI=true TZ=UTC LC_ALL=C.UTF-8 NODE_ENV=test FORCE_COLOR=0 NO_COLOR=1
export NODE_OPTIONS="--max-old-space-size=4096\""""


_RUN_TESTS = """status=0
TEST_FILES=$(find scripts -maxdepth 1 -type f -name '*.test.mjs' | LC_ALL=C sort)
for f in $TEST_FILES; do
    log="/tmp/msb-node-test-$(basename "$f").log"
    rc=0
    MSB_TEST_FILE="$f" timeout --kill-after=30 900 \\
        node --test --test-concurrency=1 \\
        --test-reporter=/home/emit_testcases.mjs --test-reporter-destination=stdout \\
        "$f" < /dev/null > "$log" 2>&1 || rc=$?
    cat "$log"
    if [ "$rc" -ne 0 ]; then
        status=1
        if ! grep -qF "TESTCASE FAILED $f" "$log"; then
            printf '\\nTESTCASE FAILED %s > <file failed to run>\\n' "$f"
        fi
    fi
done
exit "$status\""""


class OsTaxonomyImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_APT_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)
        parts = [f"FROM {base_img}"]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            "WORKDIR /home/",
            "ENV NODE_EXTRA_CA_CERTS=${CA_CERT_PATH}",
            apt_command,
            "RUN git config --global --add safe.directory '*'",
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        parts.append('CMD ["/bin/bash"]')
        return "\n\n".join(parts) + "\n"


class OsTaxonomyImageDefault(Image):
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
        return OsTaxonomyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run_script(self, apply: str) -> str:
        return f"""#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
{apply}
{_RUN_ENV}

{_RUN_TESTS}
"""

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        prepare = f"""#!/bin/bash
set -e

mkdir -p /home/{repo}
cd /home/{repo}
git init -q .
git config --local advice.detachedHead false
git remote add origin "https://github.com/{self.pr.org}/{repo}.git"
fetched=0
for attempt in 1 2 3 4 5; do
    if git fetch --depth 1 --no-tags origin "{sha}"; then
        fetched=1
        break
    fi
    echo "prepare: fetch attempt ${{attempt}} failed; retrying in 15s" >&2
    sleep 15
done
test "$fetched" -eq 1
git checkout --detach {sha}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

export CI=true
export npm_config_cache=/tmp/msb-npm-cache
node --version
npm --version

npm install --ignore-scripts --no-audit --no-fund --no-package-lock || true
rm -rf /tmp/msb-npm-cache

node -e "const p = require('/home/{repo}/package.json'); const deps = Object.keys({{...p.dependencies, ...p.devDependencies, ...p.optionalDependencies, ...p.peerDependencies}}); if (deps.length) {{ console.error('prepare: unexpected dependencies: ' + deps.join(', ')); process.exit(1); }}"
test -n "$(find scripts -maxdepth 1 -type f -name '*.test.mjs')"
for f in scripts/*.mjs; do
    node --check "$f"
done
node --input-type=module -e "import {{ readdirSync, readFileSync }} from 'node:fs'; await import('node:test'); await import('node:assert/strict'); const dir = '/home/{repo}/scripts'; for (const f of readdirSync(dir).sort()) {{ if (!f.endsWith('.mjs') || f.endsWith('.test.mjs') || readFileSync(dir + '/' + f, 'utf8').startsWith('#!')) continue; await import(dir + '/' + f); console.log('prepare: loaded scripts/' + f); }} const r = await import('/home/emit_testcases.mjs'); if (typeof r.default !== 'function') process.exit(1); console.log('DEPS_OK')"
"""
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", self._run_script("")),
            File(
                ".",
                "test-run.sh",
                self._run_script("git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._run_script(
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
                ),
            ),
            File(".", "emit_testcases.mjs", _EMIT_TESTCASES_MJS),
        ]

    def dockerfile(self) -> str:
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha).strip()
        parts = [f"FROM {self.dependency().image_full_name()}"]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            copy_commands.rstrip("\n"),
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            hardening,
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        return "\n\n".join(parts) + "\n"


@Instance.register("withmarbleapp", "os-taxonomy")
class OS_TAXONOMY(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return OsTaxonomyImageDefault(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = _ANSI_RE.sub("", test_log).replace("\r\n", "\n").replace("\r", "\n")
        for line in clean_log.split("\n"):
            match = _TESTCASE_RE.match(line.strip())
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
