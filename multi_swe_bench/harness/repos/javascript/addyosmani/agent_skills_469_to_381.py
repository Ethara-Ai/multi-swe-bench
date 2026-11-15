import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "addyosmani"
_REPO = "agent-skills"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"agent_skills_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like agent_skills_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# The repo has no package.json and no dependencies: its tests are zero-dependency
# `node --test scripts/*-test.js` files. .github/workflows/test-plugin-install.yml runs
# them on Node 20 at #381's base and Node 24 at #469's base; node:test's afterEach and
# --test-reporter work on both, so one Node 24 runtime serves the whole shard.
NODE_IMAGE = "node:24.22.2-bookworm"

# Repo test files only. evals/fixtures/**.test.js are deliberately-broken sample
# projects for the skill evals, not tests of this repo.
_TEST_FILE_RE = re.compile(r"^scripts/[^/]+-test\.js$")

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -e\n\nexport CI=true\n\n"

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
    files = set()
    for a, b in re.findall(r"^diff --git a/(\S+) b/(\S+)$", pr.test_patch, re.M):
        files.update(f for f in (a, b) if _TEST_FILE_RE.match(f))
    return sorted(files)


def _test_cmd(pr: PullRequest) -> str:
    files = " ".join(_test_files(pr))
    return (
        "rm -f /tmp/node-test-results.txt\n"
        "\n"
        "# The repo's node:test suite as CI runs it (scripts/*-test.js), which includes the\n"
        "# files the test patch touches. A file the patch adds does not exist before it is\n"
        "# applied, so each stage runs the ones present at that point.\n"
        "test_files=()\n"
        f"for f in $(ls scripts/*-test.js 2>/dev/null | sort) {files}; do\n"
        '    if [ -f "$f" ] && [[ ! " ${test_files[*]} " =~ " $f " ]]; then test_files+=("$f"); fi\n'
        "done\n"
        "\n"
        'if [ "${#test_files[@]}" -gt 0 ]; then\n'
        "    rc=0\n"
        "    node --test --test-concurrency=1 --test-reporter=/home/node-test-report.mjs "
        '--test-reporter-destination=/tmp/node-test-results.txt "${test_files[@]}" || rc=$?\n'
        "    if [ ! -s /tmp/node-test-results.txt ]; then\n"
        '        echo "node --test exited with code $rc without writing /tmp/node-test-results.txt" >&2\n'
        '        exit "$(( rc == 0 ? 1 : rc ))"\n'
        "    fi\n"
        "    cat /tmp/node-test-results.txt\n"
        "else\n"
        '    echo "no node:test file of the test patch exists at this stage"\n'
        "fi\n"
    )


class AgentSkillsImageBase(Image):
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

# Shard-wide toolchain: node and npm come with the runtime image; git and
# ca-certificates are made explicit (git also backs run-evals-test.js fixtures).
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

# Full-history clone, kept intact: no checkout and no scrub here, both are per-PR.
RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class AgentSkillsImageDefault(Image):
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
        return AgentSkillsImageBase(self.pr, self._config)

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
            "# No package.json at these commits: the scripts and their tests use only Node\n"
            "# built-ins, so there is nothing to install. The result reporter the run scripts\n"
            "# use is written outside the repo.\n"
            "cat > /home/node-test-report.mjs <<'__NODE_TEST_REPORT_EOF__'\n"
            f"{node_test_report}"
            "__NODE_TEST_REPORT_EOF__\n"
            "\n"
            "node --version\n"
            "git --version\n"
            "\n"
            "# --- 3. gate ---\n"
            "# No dependency manifest exists to install from, the repo's scripts load with\n"
            "# Node built-ins alone, and the node:test reporter loads under node --test.\n"
            "test ! -e package.json\n"
            "test -s /home/node-test-report.mjs\n"
            "for s in scripts/*.js; do node --check \"$s\"; done\n"
            "rm -rf /tmp/node-test-gate && mkdir -p /tmp/node-test-gate\n"
            "printf \"const test = require('node:test');\\ntest('gate', () => {});\\n\" "
            "> /tmp/node-test-gate/gate-test.js\n"
            "node --test --test-reporter=/home/node-test-report.mjs "
            "--test-reporter-destination=/tmp/node-test-gate/out.txt "
            "/tmp/node-test-gate/gate-test.js\n"
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

RUN cd /home/{self.pr.repo} && test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"

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
@Instance.register(_ORG, f"{_ERA_HI}-{_ERA_LO}")
class AGENT_SKILLS_469_TO_381(Instance):
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
        return AgentSkillsImageDefault(self.pr, self._config)

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
