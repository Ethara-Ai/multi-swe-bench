import re
from pathlib import Path
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "nodejs"
_REPO = "node"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"node_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like node_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

# Node.js is built from source at every base commit. The base commits of this era
# (2026-06-15 .. 2026-10-12, NODE_MAJOR_VERSION 27) all require, per BUILDING.md,
# "gcc and g++ >= 13.2 or clang >= 19.1" and a Python 3.10-3.15 for configure/gyp/
# tools/test.py. Debian 13 (trixie) ships gcc 14.2 and python 3.13; bookworm's gcc
# 12.2 is too old. The image is multi-arch (amd64 + arm64) and pinned by date.
BUILD_IMAGE = "debian:trixie-20261020"

_UPSTREAM_URL = f"https://github.com/{_ORG}/{_REPO}.git"

_SCRIPT_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"
_PREPARE_HEADER = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n\n"

# Test files the harness runs: top-level files of a tools/test.py suite.
_TEST_FILE_RE = re.compile(r"^test/([a-z0-9-]+)/(test-[^/]+)\.(?:js|mjs|cjs)$")

# tools/test.py prints TAP; this turns its result lines into the
# `PASSED|FAILED|SKIPPED <suite>/<test>` lines parse_log reads. A test name is
# tools/test.py's own reporting name (path under test/, extension stripped), so it
# carries no timing or counter and is identical in every stage.
_TAP_REPORT_PY = r"""import re
import sys

path = sys.argv[1]
plan = None
results = 0
result_re = re.compile(r'^(not ok|ok) \d+ (\S+)(.*)$')
with open(path, encoding='utf-8', errors='replace') as fh:
    for line in fh:
        line = line.rstrip('\n')
        m = re.match(r'^1\.\.(\d+)$', line)
        if m:
            plan = int(m.group(1))
            continue
        m = result_re.match(line)
        if not m:
            continue
        status, name, rest = m.groups()
        if status == 'not ok':
            word = 'FAILED'
        elif re.match(r'\s*#\s*skip\b', rest, re.I):
            word = 'SKIPPED'
        else:
            word = 'PASSED'
        results += 1
        print(word + ' ' + name)
print('node-tap-report: plan=%s results=%d' % (plan, results))
sys.exit(0 if results > 0 else 1)
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


def _patch_files(patch: str) -> list[tuple[str, bool]]:
    """(path, is_new_file) for every file a patch touches."""
    out = []
    for chunk in re.split(r"^(?=diff --git )", patch, flags=re.M):
        match = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if match:
            out.append(
                (match.group(1), re.search(r"^new file mode ", chunk, re.M) is not None)
            )
    return out


def _test_targets(pr: PullRequest) -> tuple[list[tuple[str, str]], list[str]]:
    """The tools/test.py targets of this PR's test patch.

    Returns ([(test.py name, file path)], [wildcard patterns]). A file target is
    passed to tools/test.py only when its file exists at that stage, because a
    file the test patch adds is absent before the patch is applied. When the test
    patch only ADDS test files, the existing tests of the same area
    (`<suite>/test-<area>-*`) are run as well, so the unpatched baseline stage
    still runs real tests and the new tests are measured against their siblings.
    """
    files = []
    only_new = True
    for path, is_new in _patch_files(pr.test_patch):
        match = _TEST_FILE_RE.match(path)
        if not match:
            continue
        files.append((f"{match.group(1)}/{match.group(2)}", path))
        only_new = only_new and is_new
    files = sorted(set(files))
    patterns = []
    if files and only_new:
        for name, _ in files:
            suite, test = name.split("/", 1)
            area = test.split("-")[1] if test.count("-") >= 2 else None
            if area:
                patterns.append(f"{suite}/test-{area}-*")
        # Each pattern also matches the new file it was derived from.
        files = [
            (name, path)
            for name, path in files
            if not any(re.fullmatch(p.replace("*", ".*"), name) for p in patterns)
        ]
    return files, sorted(set(patterns))


def _test_cmd(pr: PullRequest) -> str:
    files, patterns = _test_targets(pr)
    file_list = " ".join(f"{name}:{path}" for name, path in files)
    pattern_list = " ".join(f"'{p}'" for p in patterns)
    return (
        "# Rebuild out/Release/node from the tree as it is now (incremental: only what\n"
        "# the applied patches changed is recompiled; lib/ is embedded in the binary).\n"
        'make -j"${NODE_MAKE_JOBS:-4}"\n'
        "test -x out/Release/node\n"
        "\n"
        "# Only the test files the test patch touches (plus, if it only adds files,\n"
        "# their existing siblings), each passed only if it exists at this stage.\n"
        "targets=()\n"
        f"for entry in {file_list}; do\n"
        '    name="${entry%%:*}"\n'
        "    # a .js and a .mjs test of one stem share a tools/test.py name: pass it once\n"
        '    if [ -f "${entry#*:}" ] && [[ " ${targets[*]} " != *" $name "* ]]; then\n'
        '        targets+=("$name")\n'
        "    fi\n"
        "done\n"
        f"for pattern in {pattern_list}; do\n"
        '    targets+=("$pattern")\n'
        "done\n"
        'if [ "${#targets[@]}" -eq 0 ]; then\n'
        '    echo "no test file of the test patch exists at this stage"\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "rm -f /tmp/node-test.tap\n"
        "rc=0\n"
        'python3 tools/test.py -p tap --mode=release -j "${NODE_TEST_JOBS:-4}" '
        '"${targets[@]}" | tee /tmp/node-test.tap || rc=$?\n'
        "python3 /home/node-tap-report.py /tmp/node-test.tap || {\n"
        '    echo "tools/test.py exited with code $rc without reporting any test" >&2\n'
        '    exit "$(( rc == 0 ? 1 : rc ))"\n'
        "}\n"
    )


class NodeImageBase(Image):
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
        return BUILD_IMAGE

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

        # Toolchain-only base (single-commit-fetch shape): no clone here. Each PR's
        # prepare.sh fetches exactly its own base commit with --depth 1, so the base
        # is identical for every PR of the era and never holds repository history.
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

# Build toolchain for Node.js from source: gcc/g++ 14 + make (build-essential),
# python3 (configure, gyp, tools/test.py), git (fetch + patch application).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

CMD ["/bin/bash"]
"""


class NodeImageDefault(Image):
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
        return NodeImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _prepare_sh(self) -> str:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return (
            f"{_PREPARE_HEADER}"
            "# --- 1. acquire + pin: exactly this PR's base commit, detached ---\n"
            f"mkdir -p /home/{repo}\n"
            f"cd /home/{repo}\n"
            "if ! git rev-parse --verify -q HEAD > /dev/null 2>&1; then\n"
            "    git init -q\n"
            f"    git remote add origin {_UPSTREAM_URL}\n"
            "    for attempt in 1 2 3; do\n"
            f"        git fetch --depth 1 --no-tags origin {sha} && break\n"
            '        if [ "$attempt" -eq 3 ]; then exit 1; fi\n'
            '        echo "prepare: fetch attempt $attempt failed; retrying in 15s"\n'
            "        sleep 15\n"
            "    done\n"
            "    git checkout -q --detach FETCH_HEAD\n"
            "fi\n"
            "# A re-run on a provisioned container finds the repo already here: make the\n"
            "# tree pristine again (ignored build output under out/ is kept on purpose).\n"
            "git reset -q --hard\n"
            "git clean -fdq\n"
            "bash /home/check_git_changes.sh\n"
            f"git checkout -q --detach {sha}\n"
            f'test "$(git rev-parse HEAD)" = "{sha}"\n'
            "bash /home/check_git_changes.sh\n"
            "\n"
            "# --- 2. provision: build out/Release/node at this commit ---\n"
            "cat > /home/node-tap-report.py <<'__TAP_REPORT_EOF__'\n"
            f"{_TAP_REPORT_PY}"
            "__TAP_REPORT_EOF__\n"
            "\n"
            "gcc --version | head -n 1\n"
            "python3 --version\n"
            "start=$(date +%s)\n"
            "./configure\n"
            "# amd64 cross-built on an arm64 host compiles under QEMU user-mode emulation,\n"
            "# several times slower than native; the native arm64 half of the same build\n"
            "# finishes long before it, so there the compile uses every core instead of 4.\n"
            'jobs="${NODE_MAKE_JOBS:-4}"\n'
            'if [ -z "${NODE_MAKE_JOBS:-}" ] && [ "$(uname -m)" = x86_64 ] && grep -q "^CPU implementer" /proc/cpuinfo; then\n'
            '    jobs=$(nproc)\n'
            '    echo "prepare: amd64 under QEMU emulation; compiling with -j$jobs"\n'
            "fi\n"
            'make -j"$jobs"\n'
            'echo "prepare: configure + make took $(( $(date +%s) - start ))s"\n'
            "\n"
            "# --- 3. gate ---\n"
            "# The built binary is this commit's version and loads the subsystems the\n"
            "# graded tests use; tools/test.py runs a real test through the TAP reporter.\n"
            "test -x out/Release/node\n"
            "out/Release/node -p \"process.version + ' ' + process.arch\"\n"
            "out/Release/node -e \"for (const m of ['node:http', 'node:crypto', 'node:net', "
            "'node:sqlite', 'node:timers', 'node:async_hooks']) require(m); "
            "console.log('builtins ok')\"\n"
            "major=$(sed -n 's/^#define NODE_MAJOR_VERSION \\([0-9][0-9]*\\)$/\\1/p' src/node_version.h)\n"
            "out/Release/node -p 'process.versions.node' | grep -q \"^${major}\\.\"\n"
            "python3 tools/test.py -p tap --mode=release parallel/test-process-versions "
            "| tee /tmp/node-gate.tap\n"
            "python3 /home/node-tap-report.py /tmp/node-gate.tap | tee /tmp/node-gate.txt\n"
            "grep -qx 'PASSED parallel/test-process-versions' /tmp/node-gate.txt\n"
            "rm -f /tmp/node-gate.tap /tmp/node-gate.txt\n"
            'echo "DEPS_OK"\n'
        )

    def _reset(self) -> str:
        return f"cd /home/{self.pr.repo}\ngit reset -q --hard\ngit clean -fdq\n"

    def _run_sh(self) -> str:
        return f"{_SCRIPT_HEADER}{self._reset()}{_test_cmd(self.pr)}"

    def _test_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}{self._reset()}"
            "git apply --whitespace=nowarn /home/test.patch\n"
            f"{_test_cmd(self.pr)}"
        )

    def _fix_run_sh(self) -> str:
        return (
            f"{_SCRIPT_HEADER}{self._reset()}"
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
        repo = self.pr.repo

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

RUN cd /home/{repo} && test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"

WORKDIR /home/{repo}

{Image._HARDENING_BLOCK}

{self.clear_env}

# The image reaches its users as layer tars (multi-arch export, ECR pull), which gives
# every file a new inode and ctime. Compare only whole-second mtime and size, so the
# `git reset --hard` in run.sh/test-run.sh/fix-run.sh does not rewrite the whole tree
# and force a full rebuild of out/Release. Kept last so the compiled layers stay cached.
RUN git config --local core.checkStat minimal && git config --local core.trustctime false

"""


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA_FILE)
class NODE_65787_TO_53443(Instance):
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
        return NodeImageDefault(self.pr, self._config)

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

        result_re = re.compile(r"^(PASSED|FAILED|SKIPPED) ([a-z0-9-]+/test-\S+)$")

        for line in log.splitlines():
            m = result_re.match(line.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2)
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # tools/test.py names a test by its path without extension, so a .js and a
        # .mjs test of the same stem share one name; the name then fails if either
        # file fails (failed wins over passed and skipped).
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
