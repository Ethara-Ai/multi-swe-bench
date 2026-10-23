from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


NUMBER_INTERVAL = "OpenHands_10424_to_3642"

BASE_TAG = "base-10424-to-3642"

VITEST_START = "-----MSB_VITEST_JSON_START-----"
VITEST_END = "-----MSB_VITEST_JSON_END-----"
PYTEST_START = "-----MSB_PYTEST_START-----"
PYTEST_END = "-----MSB_PYTEST_END-----"

VITEST_JSON = "/home/vitest-report.json"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


# The two installs, written ONCE and spliced into both prepare.sh (build time)
# and provision.sh (an act whose patch moved a manifest), so the two can never
# install differently. Only the suites this PR is graded on are provisioned.
_PY_INSTALL = """    echo "install: python (poetry, python3.12)"
    poetry env use python3.12
    # NOT --no-root: the tests import `openhands` itself.
    attempt poetry install --with test
    poetry run python -c "import openhands; print('openhands import ok')"
    poetry run pytest --version"""

_FE_INSTALL = """    echo "install: frontend (npm ci, node $(node --version))"
    cd /home/__REPO__/frontend
    attempt npm ci --no-audit --no-fund
    test -d node_modules
    npx vitest --version
    cd /home/__REPO__"""

_ATTEMPT = """attempt() {
    for i in 1 2 3 4; do
        if "$@"; then return 0; fi
        echo "install: attempt $i of '$*' failed, retrying"
        sleep $((i * 15))
    done
    return 1
}"""

_PROVISION_SH = """#!/bin/bash
set -eo pipefail
export PATH="/root/.local/bin:$PATH"
cd /home/__REPO__

WHAT="${1:-all}"
PY_TARGETS=$(cat /home/py_targets.txt 2>/dev/null || true)
RUN_FE=$(cat /home/run_frontend.txt 2>/dev/null || echo no)

__ATTEMPT__

if [ -n "$PY_TARGETS" ] && { [ "$WHAT" = "all" ] || [ "$WHAT" = "python" ]; }; then
__PY_INSTALL__
fi

if [ "$RUN_FE" = "yes" ] && { [ "$WHAT" = "all" ] || [ "$WHAT" = "frontend" ]; }; then
__FE_INSTALL__
fi
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/__REPO__

for p in "$@"; do
    if git apply --whitespace=nowarn "$p"; then
        echo "apply_patch: applied $p"
    else
        echo "apply_patch: plain apply failed for $p, retrying with --3way"
        git apply --3way --whitespace=nowarn "$p"
        echo "apply_patch: applied $p with --3way"
    fi
    git add -A
done

if git grep -qI -e '^<<<<<<< ' -- . 2>/dev/null; then
    echo "apply_patch: conflict markers survived the merge"
    git grep -nI -e '^<<<<<<< ' -- . | head
    exit 1
fi

# The environment was provisioned from the pre-patch manifests; a patch that
# moves one would leave the acts importing packages that are declared but
# absent.
if git diff --name-only HEAD -- pyproject.toml poetry.lock | grep -q .; then
    echo "apply_patch: python manifests changed, re-provisioning"
    git diff --name-only HEAD -- pyproject.toml poetry.lock
    bash /home/provision.sh python
fi
if git diff --name-only HEAD -- frontend/package.json frontend/package-lock.json | grep -q .; then
    echo "apply_patch: frontend manifests changed, re-provisioning"
    git diff --name-only HEAD -- frontend/package.json frontend/package-lock.json
    bash /home/provision.sh frontend
fi
"""


_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export PATH="/root/.local/bin:$PATH"

_t0=$(date +%s)
step() { echo "prepare[+$(( $(date +%s) - _t0 ))s] $*"; }

cd /home/__REPO__

git reset --hard
git clean -fdq -e node_modules -e frontend/node_modules
step "checking work tree"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "__SHA__"
echo "prepare: HEAD is __SHA__"

PY_TARGETS=$(cat /home/py_targets.txt 2>/dev/null || true)
RUN_FE=$(cat /home/run_frontend.txt 2>/dev/null || echo no)

__ATTEMPT__

if [ -n "$PY_TARGETS" ]; then
    step "python install"
__PY_INSTALL__
fi

if [ "$RUN_FE" = "yes" ]; then
    step "frontend install"
__FE_INSTALL__
fi

node --version
python3.12 --version

git reset --hard
git clean -fdq -e node_modules -e frontend/node_modules
bash /home/check_git_changes.sh
step "gates passed"
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"

cd /home/__REPO__


PY_TARGETS=$(cat /home/py_targets.txt 2>/dev/null || true)
RUN_FE=$(cat /home/run_frontend.txt 2>/dev/null || echo no)

echo "__PY_START__"
if [ -n "$PY_TARGETS" ]; then
    EXISTING=""
    for f in $PY_TARGETS; do
        [ -f "$f" ] && EXISTING="$EXISTING $f"
    done
    if [ -n "$EXISTING" ]; then
        echo "pytest targets:$EXISTING"
        rc=0
        # --continue-on-collection-errors: one module that cannot import must
        # not discard every other module's results.
        timeout --signal=KILL 480 \\
            poetry run pytest -v --tb=short --no-header -p no:cacheprovider \\
            -p no:randomly --continue-on-collection-errors $EXISTING || rc=$?
        [ "$rc" = "137" ] && echo "pytest: KILLED by the wall-clock ceiling"
        echo "pytest exit: $rc"
    else
        echo "pytest: none of the target files exist at this tree state"
    fi
else
    echo "pytest: this PR's test patch adds no python unit test, suite not selected"
fi
echo "__PY_END__"

echo "__FE_START__"
if [ "$RUN_FE" = "yes" ]; then
    rm -f __VITEST_JSON__
    cd /home/__REPO__/frontend
    # src/i18n/declaration.ts is GENERATED (gitignored) from the translation
    # JSON by `make-i18n`; every component importing "#/i18n/declaration"
    # fails to load without it. Generated here, after the patches, so keys a
    # patch adds are present. Hard step: a failure must not read as 0 tests.
    if node -e "process.exit(require('./package.json').scripts['make-i18n'] ? 0 : 1)"; then
        npm run make-i18n --silent
        test -s src/i18n/declaration.ts
    fi
    rc=0
    # Two workers: these suites assert on async UI state with timers, and the
    # harness runs several acts at once; a worker per CPU starves them.
    timeout --signal=KILL 480 \\
        npx vitest run --maxWorkers=2 --minWorkers=1 --reporter=json \\
        --outputFile=__VITEST_JSON__ > /tmp/vitest_stdout 2>&1 || rc=$?
    [ "$rc" = "137" ] && echo "vitest: KILLED by the wall-clock ceiling"
    tail -40 /tmp/vitest_stdout
    echo "vitest exit: $rc"
    if [ -f __VITEST_JSON__ ]; then
        node -e '
            var r = require("__VITEST_JSON__");
            var out = {testResults: (r.testResults || []).map(function (s) {
                return {name: s.name, status: s.status,
                        assertionResults: (s.assertionResults || []).map(function (t) {
                            return {ancestorTitles: t.ancestorTitles, title: t.title, status: t.status};
                        })};
            })};
            process.stdout.write(JSON.stringify(out, null, 1));
        ' || echo "FATAL: could not re-emit the vitest report"
    else
        echo "FATAL: vitest produced no JSON report -- the runner failed to start."
    fi
    cd /home/__REPO__
else
    echo "vitest: this PR's test patch adds no frontend test, suite not selected"
fi
echo "__FE_END__"
"""


def _render(text: str, **kw: str) -> str:
    for k, v in kw.items():
        text = text.replace(f"__{k}__", v)
    return text


class OpenHands10424To3642ImageBase(Image):
    """Shared base: both toolchains plus a full clone, pinned to nothing.

    The repo is polyglot, so the base is ubuntu with python 3.12 (every commit
    here asks for ^3.11 or ^3.12) and node 22 (the frontend's .nvmrc from the
    2025 commits; the 2024 frontend declares >=14.8). Poetry 1.8.5 is what the
    repo's Makefile accepts (>= 1.8). The base holds no checkout and no prune;
    its layers carry full history, so the base tag is never published.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        repo = self.pr.repo

        # The BuildKit directive on line 1 makes DockerfileEnhancer return this
        # text verbatim, so its infrastructure block is taken from it directly.
        infra = DockerfileEnhancer._infrastructure_block(self, image_name).strip()

        parts = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            infra,
        ]
        if self.global_env:
            parts.append(self.global_env)
        parts += [
            "WORKDIR /home/",
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    ca-certificates \\\n"
            "    curl \\\n"
            "    git \\\n"
            "    gnupg \\\n"
            "    build-essential \\\n"
            "    software-properties-common \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN add-apt-repository ppa:deadsnakes/ppa -y && \\\n"
            "    apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    python3.12 \\\n"
            "    python3.12-venv \\\n"
            "    python3.12-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN mkdir -p /etc/apt/keyrings && \\\n"
            "    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \\\n"
            "        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \\\n"
            "    echo \"deb [signed-by=/etc/apt/keyrings/nodesource.gpg] "
            "https://deb.nodesource.com/node_22.x nodistro main\" \\\n"
            "        > /etc/apt/sources.list.d/nodesource.list && \\\n"
            "    apt-get update && apt-get install -y --no-install-recommends nodejs \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN curl -sSL https://install.python-poetry.org -o /tmp/install-poetry.py && \\\n"
            "    python3.12 /tmp/install-poetry.py --version 1.8.5 && \\\n"
            "    rm -f /tmp/install-poetry.py",
            'ENV PATH="/root/.local/bin:$PATH"',
            "RUN git config --global --add safe.directory '*'",
            # Copied from OpenHands_12354_to_9271's base: `git -C /home clone`
            # (never the literal `git clone`, the enhancer keys on it), the
            # build-arg URL, a retry loop with `rm -rf`, and a final `test -d`.
            "RUN set -eux; \\\n"
            "    for i in 1 2 3 4 5; do \\\n"
            f"        rm -rf /home/{repo}; \\\n"
            f"        if git -C /home clone \"${{REPO_URL}}\" {repo}; then break; fi; \\\n"
            "        echo \"clone attempt $i failed, retrying\"; sleep 15; \\\n"
            "    done; \\\n"
            f"    test -d /home/{repo}/.git",
        ]
        if self.clear_env:
            parts.append(self.clear_env)
        parts.append('CMD ["/bin/bash"]')
        return "\n\n".join(parts) + "\n"


class OpenHands10424To3642ImageDefault(Image):
    """Per-PR layer: checkout of its base commit (fetched by sha only if the
    clone lacks it), derived prune, then prepare.sh."""

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
        return OpenHands10424To3642ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # Which suites a PR needs is decided from its OWN test patch, which is
    # baked into the image and identical across the three acts.
    def _test_files(self) -> list[str]:
        return [ln[6:].strip() for ln in self.pr.test_patch.split("\n")
                if ln.startswith("+++ b/")]

    def py_targets(self) -> list[str]:
        """pytest modules under tests/, excluding tests/runtime/.

        tests/runtime/ drives a live sandbox runtime (a docker container the
        suite starts itself); the act container has no docker daemon, so
        those modules can only error identically in every act.
        """
        out = set()
        for f in self._test_files():
            name = f.rsplit("/", 1)[-1]
            if not f.startswith("tests/") or f.startswith("tests/runtime/"):
                continue
            if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
                out.add(f)
        return sorted(out)

    def needs_frontend(self) -> bool:
        return any(f.startswith("frontend/") and re.search(r"\.(test|spec)\.[jt]sx?$", f)
                   for f in self._test_files())

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        py = self.py_targets()
        fe = self.needs_frontend()
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "py_targets.txt", "\n".join(py) + ("\n" if py else "")),
            File(".", "run_frontend.txt", "yes\n" if fe else "no\n"),
            File(".", "check_git_changes.sh", _render(_CHECK_GIT_CHANGES_SH, REPO=repo)),
            File(".", "provision.sh", _render(_render(_PROVISION_SH, ATTEMPT=_ATTEMPT,
                                                      PY_INSTALL=_PY_INSTALL,
                                                      FE_INSTALL=_FE_INSTALL), REPO=repo)),
            File(".", "apply_patch.sh", _render(_APPLY_PATCH_SH, REPO=repo)),
            File(".", "prepare.sh", _render(_render(_PREPARE_SH, ATTEMPT=_ATTEMPT,
                                                    PY_INSTALL=_PY_INSTALL,
                                                    FE_INSTALL=_FE_INSTALL), REPO=repo, SHA=sha)),
            File(".", "run_tests.sh", _render(_RUN_TESTS_SH, REPO=repo,
                                              PY_START=PYTEST_START, PY_END=PYTEST_END,
                                              FE_START=VITEST_START, FE_END=VITEST_END,
                                              VITEST_JSON=VITEST_JSON)),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency().image_full_name()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # Literal sha: the BASE_COMMIT build arg reaches only the base image.
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    if ! git cat-file -e "{sha}^{{commit}}" 2>/dev/null; then \\
        for i in 1 2 3 4 5; do \\
            if git fetch --depth 1 --no-tags -q origin "{sha}"; then break; fi; \\
            echo "fetch {sha} attempt $i failed, retrying"; sleep 10; \\
        done; \\
    fi; \\
    git cat-file -e "{sha}^{{commit}}"

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


@Instance.register("OpenHands", NUMBER_INTERVAL)
class OpenHands10424To3642(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenHands10424To3642ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        """Both suites, each read from its own delimited section.

        Ids are namespaced by runner (`pytest::` / `vitest::`) so they cannot
        collide, and so report.py's file-path guard never reads the id head as
        a file the fix patch touched.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # pytest writes a parametrised id verbatim and a parameter may contain
        # spaces, so the id is matched lazily up to a status word that is
        # followed only by the optional `[ NN%]` progress column.
        line_re = re.compile(
            r"^(?P<name>\S[^\s:]*::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?=\s*(?:\[\s*\d+%\])?\s*$)")
        for line in self._between(text, PYTEST_START, PYTEST_END).splitlines():
            m = line_re.match(line.strip())
            if not m:
                continue
            name = uniq(f"pytest::{m.group('name')}")
            st = m.group("status")
            if st in ("PASSED", "XPASS"):
                passed.add(name)
            elif st in ("FAILED", "ERROR"):
                failed.add(name)
            else:
                skipped.add(name)

        fe = self._between(text, VITEST_START, VITEST_END)
        anchor = fe.find('"testResults"')
        starts = [m.start() for m in re.finditer(r"\{", fe)]
        if anchor != -1:
            starts.sort(key=lambda i: (i > anchor, abs(anchor - i)))
        data = None
        for start in starts:
            for end in range(len(fe), start, -1):
                chunk = fe[start:end]
                if not chunk.rstrip().endswith("}"):
                    continue
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "testResults" in parsed:
                    data = parsed
                break
            if data is not None:
                break
        if data is not None:
            for suite in data.get("testResults", []) or []:
                fname = (suite.get("name") or "").split("/frontend/", 1)[-1]
                for t in suite.get("assertionResults", []) or []:
                    full = " > ".join((t.get("ancestorTitles") or []) + [t.get("title") or ""])
                    name = uniq(f"vitest::{fname}::{full}".strip())
                    st = (t.get("status") or "").lower()
                    if st == "passed":
                        passed.add(name)
                    elif st in ("failed", "error"):
                        failed.add(name)
                    else:
                        skipped.add(name)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
