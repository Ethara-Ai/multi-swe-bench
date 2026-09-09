from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


BUNDLE_PR_NUMBERS = [
    9271, 9432, 9447, 9458, 9648, 9942, 11274, 11710, 11862, 11950,
    11982, 12036, 12111, 12153, 12180, 12223, 12224, 12237, 12300, 12354,
]

NUMBER_INTERVAL = "OpenHands_12354_to_9271"

BASE_TAG = "base-12354-to-9271"

VITEST_START = "-----MSB_VITEST_JSON_START-----"
VITEST_END = "-----MSB_VITEST_JSON_END-----"
PYTEST_START = "-----MSB_PYTEST_START-----"
PYTEST_END = "-----MSB_PYTEST_END-----"

VITEST_JSON = "/home/vitest-report.json"


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git update-index -q --really-refresh || true

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""


_APPLY_PATCH_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

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
"""


_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PATH="/root/.local/bin:$PATH"

cd /home/{repo}

PY_TARGETS=$(cat /home/py_targets.txt 2>/dev/null || true)
CLI_TARGETS=$(cat /home/cli_targets.txt 2>/dev/null || true)
RUN_FE=$(cat /home/run_frontend.txt 2>/dev/null || echo no)

echo "{py_start}"
if [ -n "$PY_TARGETS" ]; then
    EXISTING=""
    for f in $PY_TARGETS; do
        [ -f "$f" ] && EXISTING="$EXISTING $f"
    done
    if [ -n "$EXISTING" ]; then
        echo "pytest targets:$EXISTING"
        rc=0
        poetry run pytest -v --tb=short --no-header -p no:cacheprovider \\
            -p no:randomly --continue-on-collection-errors \\
                $EXISTING || rc=$?
        echo "pytest exit: $rc"
    else
        echo "pytest: none of the target files exist at this tree state"
    fi
else
    echo "pytest: this PR's test patch adds no root python test, suite not selected"
fi

# openhands-cli is a SEPARATE project: hatchling build backend, its own
# uv.lock, and dependencies (openhands-sdk, openhands-tools) that the root
# poetry environment does not and cannot provide. Running its tests from the
# root env fails collection with
#     ModuleNotFoundError: No module named 'openhands.sdk'
# and the act reports 0/0/0 - measured on pr-11274 and pr-11710.
if [ -n "$CLI_TARGETS" ]; then
    cd /home/{repo}/openhands-cli
    REL=""
    for f in $CLI_TARGETS; do
        r="${{f#openhands-cli/}}"
        [ -f "$r" ] && REL="$REL $r"
    done
    if [ -n "$REL" ]; then
        echo "pytest (openhands-cli) targets:$REL"
        rc=0
        uv run pytest -v --tb=short --no-header -p no:cacheprovider \\
            --continue-on-collection-errors $REL || rc=$?
        echo "pytest exit: $rc"
    else
        echo "pytest (openhands-cli): no target file exists at this tree state"
    fi
    cd /home/{repo}
fi
echo "{py_end}"

echo "{fe_start}"
if [ "$RUN_FE" = "yes" ]; then
    rm -f {vitest_json}
    cd /home/{repo}/frontend
    rc=0
    # --maxWorkers=2 is load-bearing, not tuning. vitest defaults to one
    # worker per CPU, and the harness runs several acts concurrently, so the
    # default oversubscribes the machine several times over. These suites
    # assert on async UI state with waitFor/timers, and under that load the
    # timers expire before the work completes: MEASURED on pr-12153, the same
    # image and act gave 748 passed / 0 failed run standalone (twice,
    # identically) and 722 passed / 26 failed when four acts ran at once.
    # Those 26 were not flaky code - they were starved CPU.
    npx vitest run --maxWorkers=2 --reporter=json --outputFile={vitest_json} \
        > /tmp/vitest_stdout 2>&1 || rc=$?
    tail -40 /tmp/vitest_stdout
    echo "vitest exit: $rc"
    if [ -f {vitest_json} ]; then
        cat {vitest_json}
    else
        echo "FATAL: vitest produced no JSON report -- the runner failed to start."
    fi
else
    echo "vitest: this PR's test patch adds no frontend test, suite not selected"
fi
echo "{fe_end}"
"""


class OpenHands12354To9271ImageBase(Image):
    """Clone-only base, shared by all twenty PRs in the bundle.

    Both toolchains live here because both are invariant across the bundle:
    node 22 (every commit's frontend asks for >=20 or >=22) and python 3.12 +
    poetry (every commit pins ^3.12,<3.14). Only the per-commit dependency
    resolution is era-dependent, and that stays in prepare.sh.

    The base stops at the clone: no checkout, no pin, no scrub. Its content is
    identical for every PR, which is what lets one tag serve all twenty.
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

    # ubuntu rather than a language image because this repo needs BOTH
    # toolchains in one image; neither node: nor python: ships the other.
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
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        repo = self.pr.repo

        # `git -C /home clone`, never the literal `git clone`: core's
        # DockerfileEnhancer._inject_final_sanitize() appends its hardening block
        # to any Dockerfile containing that substring, and under this layout the
        # block belongs in the PR layer. Same operation to git, different text.
        #
        # The retry loop guards the one step here that depends on a third party
        # being reachable, with `rm -rf` before each attempt so a half-written
        # tree is never mistaken for success, and a final `test -d` so five
        # silent failures cannot leave an empty directory behind.
        return """FROM {image_name}

{global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    gnupg \\
    build-essential \\
    software-properties-common \\
    && rm -rf /var/lib/apt/lists/*

RUN add-apt-repository ppa:deadsnakes/ppa -y && \\
    apt-get update && apt-get install -y --no-install-recommends \\
    python3.12 \\
    python3.12-venv \\
    python3.12-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /etc/apt/keyrings && \\
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \\
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \\
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] \\
https://deb.nodesource.com/node_22.x nodistro main" \\
        > /etc/apt/sources.list.d/nodesource.list && \\
    apt-get update && apt-get install -y --no-install-recommends nodejs \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org -o /tmp/install-poetry.py && \\
    python3.12 /tmp/install-poetry.py --version 1.8.5 && \\
    rm -f /tmp/install-poetry.py

ENV PATH="/root/.local/bin:$PATH"

RUN git config --global --add safe.directory '*'

RUN set -eux; \\
    for i in 1 2 3 4 5; do \\
        rm -rf /home/{repo}; \\
        if git -C /home clone "${{REPO_URL}}" {repo}; then break; fi; \\
        echo "clone attempt $i failed, retrying"; sleep 15; \\
    done; \\
    test -d /home/{repo}/.git

{clear_env}

CMD ["/bin/bash"]
""".format(
            image_name=image_name,
            global_env=self.global_env,
            clear_env=self.clear_env,
            repo=repo,
        )


class OpenHands12354To9271ImageDefault(Image):
    """Per-PR layer: context files, the checkout, the scrub, then prepare.sh."""

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
        return OpenHands12354To9271ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    # ---- which suites this PR needs, decided from its OWN test patch -------
    #
    # This repo is polyglot: of the twenty PRs, 13 touch only frontend tests,
    # 6 only python tests, and 1 touches both. Running vitest for a
    # python-only PR scores 0/0/0 and reads as "no test evidence" when the
    # truth is that the harness never ran its tests.
    #
    # test.patch is baked into the image and identical across all three acts,
    # so the selection is constant WITHIN an instance (the acts stay
    # comparable) while differing BETWEEN instances.
    def _test_files(self) -> list[str]:
        out = []
        for line in self.pr.test_patch.split("\n"):
            if line.startswith("+++ b/"):
                out.append(line[6:].strip())
        return out

    def py_targets(self) -> list[str]:
        """Python tests served by the ROOT poetry environment."""
        return sorted({f for f in self._test_files()
                       if f.endswith(".py") and not f.startswith("openhands-cli/")})

    def cli_targets(self) -> list[str]:
        """Python tests belonging to the openhands-cli sub-project (uv)."""
        return sorted({f for f in self._test_files()
                       if f.endswith(".py") and f.startswith("openhands-cli/")})

    def needs_frontend(self) -> bool:
        return any(f.startswith("frontend/") for f in self._test_files())

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        py = self.py_targets()
        cli = self.cli_targets()
        fe = self.needs_frontend()

        run_tests = _RUN_TESTS_SH.format(
            repo=repo,
            py_start=PYTEST_START, py_end=PYTEST_END,
            fe_start=VITEST_START, fe_end=VITEST_END,
            vitest_json=VITEST_JSON,
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "py_targets.txt", "\n".join(py) + ("\n" if py else "")),
            File(".", "cli_targets.txt", "\n".join(cli) + ("\n" if cli else "")),
            File(".", "run_frontend.txt", "yes\n" if fe else "no\n"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH.format(repo=repo)),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH.format(repo=repo)),
            File(".", "run_tests.sh", run_tests),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail
export PATH="/root/.local/bin:$PATH"

cd /home/{repo}

git reset --hard
git clean -fdq -e node_modules -e frontend/node_modules
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{sha}"

# Only the toolchain this PR is actually graded on is provisioned. Installing
# both for every PR would roughly double 20 image builds for no gain: a PR
# whose test patch touches no python file never runs pytest, and one that
# touches no frontend file never runs vitest.
PY_TARGETS=$(cat /home/py_targets.txt 2>/dev/null || true)
CLI_TARGETS=$(cat /home/cli_targets.txt 2>/dev/null || true)
RUN_FE=$(cat /home/run_frontend.txt 2>/dev/null || echo no)

if [ -n "$CLI_TARGETS" ]; then
    echo "provisioning openhands-cli: this PR is graded on its uv sub-project"
    # openhands-cli declares a hatchling build backend and ships its own
    # uv.lock, so it is provisioned with uv from inside that directory. The
    # installer resolves the architecture itself, which keeps multi-arch
    # working.
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="/root/.local/bin:$PATH"
    cd /home/{repo}/openhands-cli
    uv sync --frozen || uv sync
    # A HARD gate: without it a failed sync yields an image whose every act
    # scores 0/0/0 with no error.
    uv run python -c "import openhands_cli; print('openhands_cli import ok')"
    uv run pytest --version
    cd /home/{repo}
fi

if [ -n "$PY_TARGETS" ]; then
    echo "provisioning python: this PR is graded on pytest"
    poetry env use python3.12
    # Not suffixed with a status-swallowing `|| true`: this is the REAL
    # install, so a failure must fail the build rather than yield an image
    # where every act scores 0/0/0 with no error.
    # NOT --no-root: the tests import `openhands` itself, so the project
    # package has to be installed, not just its dependencies. With --no-root
    # every python act dies at collection with
    #     ModuleNotFoundError: No module named 'openhands'
    # and reports 0/0/0 -- measured on pr-9432 before this was fixed.
    poetry install --with test
    # A HARD gate, deliberately not suffixed with a status-swallowing `|| true`.
    # It was written that way once and hid exactly the failure above until an
    # act ran; a gate that cannot fail is not a gate.
    poetry run python -c "import openhands; print('openhands import ok')"
    poetry run pytest --version
fi

if [ "$RUN_FE" = "yes" ]; then
    echo "provisioning frontend: this PR is graded on vitest"
    cd /home/{repo}/frontend
    # frontend/package-lock.json is committed at every commit in this bundle,
    # so `npm ci` is available and is preferred over `npm install`: it installs
    # exactly the locked tree instead of re-resolving a moving one.
    npm ci --no-audit --no-fund
    test -d node_modules
    npx vitest --version
    cd /home/{repo}
fi

node --version
python3.12 --version

# Restore the tree for the acts, keeping the installed dependencies.
git reset --hard
git clean -fdq -e node_modules -e frontend/node_modules
bash /home/check_git_changes.sh
""".format(repo=repo, sha=sha),
            ),
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
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The sha is written LITERALLY. build_dataset.py supplies the
        # REPO_URL / BASE_COMMIT build args only when dependency() returns a
        # str -- true for the base alone -- so a PR layer writing
        # ${BASE_COMMIT} would expand it to the empty string and the unquoted
        # `git checkout` would silently land on the default branch.
        sha = self.pr.base.sha

        # The hardening block is DERIVED from the harness's own definition,
        # never retyped: retyping it once cost a missing submodule scrub that
        # no gate caught. `_HARDENING_BLOCK` is READ from harness.image;
        # nothing in core is modified.
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        return f"""FROM {image_name}

{copies}
WORKDIR /home/{self.pr.repo}

RUN git reset --hard && git checkout {sha}

{scrub}

RUN bash /home/prepare.sh
"""


class OpenHands12354To9271(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenHands12354To9271ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _between(text: str, start: str, end: str) -> str:
        i = text.find(start)
        if i == -1:
            return ""
        i += len(start)
        j = text.find(end, i)
        return text[i:j] if j != -1 else text[i:]

    def parse_log(self, test_log: str) -> TestResult:
        """Read both suites, each from its own delimited section.

        Ids are namespaced by runner so the two suites can never collide, and
        the tool name comes FIRST: report.py's cheating guard splits a test
        name on "::" and treats the head as a file path, so a head of "pytest"
        or "vitest" matches no file and a PR that CREATES a test file cannot
        trip guard_fix_patch_touched_tests.
        """
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        # Occurrence counter. A suite that builds tests in a loop, or two
        # `describe` blocks using the same title in one file, gives several
        # tests an identical fully-qualified name; without this they collapse
        # into ONE set entry and the count silently drops. Measured on
        # pr-12354: vitest reported 870 assertions, 865 distinct names -- one
        # title repeated 5 times. The count follows reporter order, which is
        # vitest's file-and-declaration order and therefore stable across the
        # three acts.
        seen: dict[str, int] = {}

        def uniq(name: str) -> str:
            n = seen[name] = seen.get(name, 0) + 1
            return name if n == 1 else f"{name} #{n}"

        text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # ---- pytest: one line per test, `path::test NAME STATUS`
        py = self._between(text, PYTEST_START, PYTEST_END)
        # `\S+` for the id is WRONG: pytest writes a parametrised id verbatim, and
        # a parameter may contain spaces --
        #     tests/x.py::test_send_pull_request[ready-None-Custom PR Title] PASSED [ 25%]
        # `\S+` stops at the space inside the bracket and the line is dropped
        # SILENTLY. Measured on pr-9942: 4 such tests per act, 12 results across the
        # three acts, invisible in every count. Match the id lazily up to the status
        # word instead, and require the status to be followed by end-of-line or
        # pytest's `[ NN%]` progress column so a status word occurring inside a test
        # name cannot terminate the id early.
        line_re = re.compile(
            r"^(?P<name>\S[^\s:]*::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?=\s*(?:\[\s*\d+%\])?\s*$)")
        for line in py.splitlines():
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

        # ---- vitest: a JSON report, read as a whole object
        fe = self._between(text, VITEST_START, VITEST_END)
        start = fe.find("{")
        if start != -1:
            for end in range(len(fe), start, -1):
                chunk = fe[start:end]
                if not chunk.rstrip().endswith("}"):
                    continue
                try:
                    data = json.loads(chunk)
                except ValueError:
                    continue
                for suite in data.get("testResults", []) or []:
                    fname = suite.get("name") or ""
                    fname = fname.split("/frontend/", 1)[-1]
                    for t in suite.get("assertionResults", []) or []:
                        full = " > ".join(
                            (t.get("ancestorTitles") or []) + [t.get("title") or ""])
                        name = uniq(f"vitest::{fname}::{full}".strip())
                        st = (t.get("status") or "").lower()
                        if st == "passed":
                            passed.add(name)
                        elif st in ("failed", "error"):
                            failed.add(name)
                        else:
                            skipped.add(name)
                break

        # TestResult.__post_init__ enforces disjoint sets: failure outranks a
        # skip, and a skip outranks a pass.
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


# The dataset carries this exact string in `number_interval`; Instance.create()
# looks a record up as f"{org}/{number_interval}". Registering only this key
# leaves every other OpenHands config — the generic `OpenHands/OpenHands` and
# the 27 era keys, several of whose ranges overlap this one — untouched.
Instance.register("OpenHands", NUMBER_INTERVAL)(OpenHands12354To9271)
