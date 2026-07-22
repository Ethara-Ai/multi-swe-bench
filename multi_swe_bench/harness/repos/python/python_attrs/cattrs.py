import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# --------------------------------------------------------------------------- #
# Era boundaries (by PR number)
#
#   Era A  PR 18         - setup.py, flat cattr/ layout, Python 3.6
#   Era B  PR 45-137     - setup.py, src/cattr/ layout, Python 3.7
#   Era C  PR 139-312    - poetry-core build, src/cattr(s)/ layout, Python 3.7
#   Era D  PR 371-688    - hatchling or poetry-core, Python 3.12
#                          (PR 371 needs typing.Final -> requires Python 3.8+)
#
# Each era has ONE shared base image that clones the repo and pre-installs the
# toolchain; the per-PR image checks out that PR's base.sha on top of it.
# --------------------------------------------------------------------------- #

# PR numbers per era (sorted)
_ERA_A = {18}
_ERA_B = {45, 86, 107, 115, 123, 132, 137}
_ERA_C = {139, 144, 167, 198, 207, 247, 312}
_ERA_D = {371, 431, 461, 543, 653, 660, 688}


def _era(pr_number: int) -> str:
    if pr_number in _ERA_A:
        return "A"
    if pr_number in _ERA_B:
        return "B"
    if pr_number in _ERA_C:
        return "C"
    if pr_number in _ERA_D:
        return "D"
    raise ValueError(f"PR #{pr_number} not mapped to any era")


# Per-era base OS image, extra build-backend packages, and pinned test deps.
_ERA_SPEC = {
    "A": {
        "base_os": "python:3.6-slim",
        "build_backend": "",
        # Pinned deps from requirements_dev.txt for this era
        "test_deps": "pytest==3.0.4 hypothesis==3.36.0 coverage==4.2",
        "extra_deps": "",
    },
    "B": {
        "base_os": "python:3.7-slim",
        "build_backend": "",
        "test_deps": '"pytest<8" "hypothesis<6.31" attrs',
        "extra_deps": "pymongo immutables",
    },
    "C": {
        # Kept on 3.7 deliberately. 3.8 was trialled because pr-144's
        # typing.Literal test is `@pytest.mark.skipif(is_py37)` and so can never
        # transition on 3.7; on 3.8 it does (pr-144 becomes valid, and 167/198/312
        # gain transitions). But un-skipping those tests also broke pr-139,
        # pr-207 and pr-247, which then trip report.py's rule-4 anomaly
        # (pass at base -> absent after test.patch -> fail after fix.patch).
        # Net 3 lost for 1 gained, so 3.7 stands. The repo declares
        # python = "^3.7", which permits either.
        "base_os": "python:3.7-slim",
        # poetry-core needed to build from pyproject.toml
        "build_backend": "poetry-core",
        # Bounds taken from this era's own pyproject.toml
        # (pytest = "^6.2.3", hypothesis = "^6.9.2").
        "test_deps": '"pytest>=6.2.3,<7" "hypothesis>=6.9.2,<7" "attrs>=20.1.0"',
        "extra_deps": "pymongo immutables",
    },
    "D": {
        "base_os": "python:3.12-slim",
        # hatchling + hatch-vcs + poetry-core for PEP 517 build (covers both)
        "build_backend": "hatchling hatch-vcs poetry-core",
        # Bounds taken from this era's own pyproject.toml
        # (pytest = "^7.1.3", hypothesis = "^6.54.5").
        "test_deps": (
            '"pytest>=7.1.3,<8" "hypothesis>=6.54.5,<7" "attrs>=20" typing-extensions'
        ),
        # Optional test deps for preconf tests
        "extra_deps": (
            "ujson orjson msgpack pyyaml tomlkit cbor2 pymongo msgspec immutables"
        ),
    },
}


# Per-PR interpreter overrides. Empty: every era currently uses its era default.
# The hook is kept because era membership is decided by build system and repo
# layout, which does not always pin a single usable interpreter.
_PR_BASE_OS_OVERRIDE: dict[int, str] = {
    # pr-144's only new test is `@pytest.mark.skipif(is_py37)` and needs
    # typing.Literal, so on era C's 3.7 base the very test the fix patch targets
    # is collected but skipped and can never transition. The repo declares
    # python = "^3.7", which permits 3.8. Overriding only this record keeps the
    # shared 3.7 base-c (and pr-139/207/247, which regress on 3.8) untouched.
    144: "python:3.8-slim",
}


def _base_os_for(pr_number: int, era: str) -> str:
    return _PR_BASE_OS_OVERRIDE.get(pr_number, _ERA_SPEC[era]["base_os"])


def _base_tag_for(pr_number: int, era: str) -> str:
    """Base image tag. Overridden PRs get their own base so they do not collide
    with the era's shared base image."""
    tag = f"base-{era.lower()}"
    if pr_number in _PR_BASE_OS_OVERRIDE:
        ver = _PR_BASE_OS_OVERRIDE[pr_number].split(":")[1].split("-")[0]
        tag = f"{tag}-py{ver.replace('.', '')}"
    return tag


# ---- shared shell helpers ------------------------------------------------- #

# The dataset's patches were produced without `git diff --binary`, so binary
# files appear as contentless "Binary files ... differ" stubs that git apply can
# never apply. Drop those file sections rather than let one stub abort the whole
# patch.
_FILTER_PATCH_PY = (
    "import sys\n"
    "\n"
    "if len(sys.argv) < 2:\n"
    "    sys.exit(1)\n"
    "\n"
    "try:\n"
    "    content = open(sys.argv[1]).read()\n"
    "except (IOError, OSError):\n"
    "    sys.exit(1)\n"
    "\n"
    "if not content.strip():\n"
    "    sys.exit(1)\n"
    "\n"
    "# Split on 'diff --git' boundaries by hand. re.split() with a zero-width\n"
    "# lookahead raises ValueError on Python < 3.7, which era A (3.6) still uses.\n"
    "parts = []\n"
    "current = []\n"
    "for line in content.splitlines(True):\n"
    "    if line.startswith('diff --git ') and current:\n"
    "        parts.append(''.join(current))\n"
    "        current = [line]\n"
    "    else:\n"
    "        current.append(line)\n"
    "if current:\n"
    "    parts.append(''.join(current))\n"
    "\n"
    "filtered = [p for p in parts if p.strip() and 'Binary files' not in p]\n"
    "result = ''.join(filtered)\n"
    "\n"
    "if result.strip():\n"
    "    sys.stdout.write(result)\n"
    "else:\n"
    "    sys.exit(1)\n"
)

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


def _prepare_sh(repo: str, base_sha: str) -> str:
    return f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh
pip install --no-cache-dir -e .
"""


# A patch that fails to apply must abort the stage loudly. Swallowing the error
# makes the stage silently identical to the previous one, which reads as a
# genuine "no transition" result when it is really a tooling failure.
_APPLY_TEST_PATCH = """if [ -s /home/test.patch ]; then
    python3 /home/filter_patch.py /home/test.patch > /tmp/filtered_test.patch || \\
        { echo "PATCH_FILTER_FAILED: test.patch"; exit 1; }
    git apply --whitespace=nowarn /tmp/filtered_test.patch || \\
        { echo "PATCH_APPLY_FAILED: test.patch"; exit 1; }
fi"""

_APPLY_FIX_PATCH = """if [ -s /home/fix.patch ]; then
    python3 /home/filter_patch.py /home/fix.patch > /tmp/filtered_fix.patch || \\
        { echo "PATCH_FILTER_FAILED: fix.patch"; exit 1; }
    git apply --whitespace=nowarn /tmp/filtered_fix.patch || \\
        { echo "PATCH_APPLY_FAILED: fix.patch"; exit 1; }
fi"""


def _run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{test_cmd}
"""


def _test_run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{_APPLY_TEST_PATCH}
{test_cmd}
"""


def _fix_run_sh(repo: str, test_cmd: str) -> str:
    return f"""#!/bin/bash
set -eo pipefail
cd /home/{repo}
git checkout -- . 2>/dev/null || true
{_APPLY_TEST_PATCH}
{_APPLY_FIX_PATCH}
{test_cmd}
"""


def _test_cmd(era: str) -> str:
    """pytest invocation for an era.

    NOTE: no -x. Stopping at the first failure would abort the run stage as soon
    as an f2p test fails, leaving every later test unexecuted (status NONE) and
    wrecking the p2p/f2p/n2p classification.

    --continue-on-collection-errors is equally load-bearing: cattrs' optional
    preconf backends (bson/msgpack/ujson/...) are not all installable in every
    era, and without the flag a single un-importable test module aborts the
    whole session ("Interrupted: N errors during collection") so the stage
    reports zero tests. A zero-result run stage then makes the fix stage's
    entire passing suite look like new tests, inflating n2p.
    """
    # --hypothesis-seed pins the property-test example generator. cattrs' suite
    # is heavily @given-based, and with a random seed a test can pass in the test
    # stage and fail in the fix stage purely by chance. report.py treats
    # PASS-then-FAIL as a fix-introduced regression and invalidates the whole
    # record, so an unpinned seed silently discards good records at random.
    if era == "A":
        # pytest 3.0.4 predates some -o handling; keep the invocation minimal.
        return (
            "python -m pytest tests/ --tb=short -v"
            " --continue-on-collection-errors --hypothesis-seed=0"
        )
    # Neutralise any addopts the repo sets (coverage, -x, ...) so the run is
    # reproducible and nothing aborts early.
    return (
        'python -m pytest tests/ --tb=short -v -o "addopts="'
        " --continue-on-collection-errors --hypothesis-seed=0"
    )


# --------------------------------------------------------------------------- #
#  Shared per-era base image (reference format)
#
#  The leading `# syntax` directive opts out of DockerfileEnhancer, which would
#  otherwise rewrite this file and inject proxy/CA scaffolding. Hardening is
#  therefore written by hand: light here (full history is kept at the base) and
#  strict in the PR layer.
# --------------------------------------------------------------------------- #
class ImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config, era: str):
        self._pr = pr
        self._config = config
        self._era = era

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def era(self) -> str:
        return self._era

    def dependency(self) -> Union[str, "Image"]:
        return _base_os_for(self.pr.number, self.era)

    def image_tag(self) -> str:
        return _base_tag_for(self.pr.number, self.era)

    def workdir(self) -> str:
        return _base_tag_for(self.pr.number, self.era)

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        spec = _ERA_SPEC[self.era]
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        backend = (
            f"RUN pip install --no-cache-dir {spec['build_backend']}\n"
            if spec["build_backend"]
            else ""
        )
        extra = (
            f"RUN pip install --no-cache-dir {spec['extra_deps']} || true\n"
            if spec["extra_deps"]
            else ""
        )

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

{self.global_env}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

{backend}RUN pip install --no-cache-dir {spec['test_deps']}
{extra}
{code}

WORKDIR /home/{self.pr.repo}
RUN git remote remove origin 2>/dev/null || true; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""
WORKDIR /home/

{self.clear_env}

CMD ["/bin/bash"]
"""


# --------------------------------------------------------------------------- #
#  Per-PR image: FROM the era base, check out base.sha, then harden.
# --------------------------------------------------------------------------- #
class ImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._era = _era(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config, self._era)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = _test_cmd(self._era)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "filter_patch.py", _FILTER_PATCH_PY),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(self.pr.repo, self.pr.base.sha)),
            File(".", "run.sh", _run_sh(self.pr.repo, test_cmd)),
            File(".", "test-run.sh", _test_run_sh(self.pr.repo, test_cmd)),
            File(".", "fix-run.sh", _fix_run_sh(self.pr.repo, test_cmd)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # Canonical hardening from image.py, pinned to this PR's literal base.sha
        # (the PR image has an Image-typed dependency, so the enhancer returns raw).
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}

CMD ["/bin/bash"]
"""


# --------------------------------------------------------------------------- #
#  Instance - single registration for all 22 PRs
# --------------------------------------------------------------------------- #
@Instance.register("python-attrs", "cattrs")
class Cattrs(Instance):
    """Evaluation instance for python-attrs/cattrs.

    Handles all 22 LHT instances across versions v0.6.0 through v26.1.0.
    Routes here when both number_interval and tag are empty; the bundle
    number_interval keys registered below route the delivery form.
    Dispatches to the appropriate era base image based on PR number.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
        """Parse pytest output.

        Handles standard pytest verbose format:
            tests/test_foo.py::test_bar PASSED
        Also handles the reversed format and ANSI color codes.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI escape codes
        log_clean = re.sub(r"\x1b\[.*?m", "", log)

        # Match: test_path STATUS  or  STATUS test_path
        # The node id is matched loosely so parametrised ids (which may contain
        # commas, dashes and quotes) are not truncated or dropped.
        pattern = re.compile(
            r"(tests/\S+\.py::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR)"
            r"|(PASSED|FAILED|SKIPPED|ERROR)\s+(tests/\S+\.py::\S+)"
        )

        for match in pattern.finditer(log_clean):
            if match.group(1):
                test_name = match.group(1).strip()
                status = match.group(2)
            elif match.group(3):
                status = match.group(3)
                test_name = match.group(4).strip()
            else:
                continue

            if status == "PASSED":
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status == "SKIPPED":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Data-derived from Dataset/python-attrs__cattrs_lht_final.jsonl.
# The JSONL currently carries no number_interval, so records route by
# "python-attrs/cattrs"; these keys are what the delivery form routes on once
# number_interval is backfilled. Regenerate when bundles change.
_BUNDLE_NIS_Cattrs = [
    "18-24-25-28",  # lead pr-18
    "45-55-56-62-66-73",  # lead pr-45
    "86-98",  # lead pr-86
    "107-108",  # lead pr-107
    "115-117",  # lead pr-115
    "123-124-126",  # lead pr-123
    "132-133-135",  # lead pr-132
    "137-141-142-143",  # lead pr-137
    "139-152-153-157-162",  # lead pr-139
    "144-146",  # lead pr-144
    "167-177-179-182-187-191-193",  # lead pr-167
    "198-203",  # lead pr-198
    "207-210-212-213-219-221-224-225-227-229-231",  # lead pr-207
    "247-250-251-255-266-276-283-284-285-286-291-295-298-302-303-304-306-309-310",  # lead pr-247
    "312-313-314-318-323-326-327-328-330-334-337-341-342-343-344-349-351-353-355-363-364-366-367",  # lead pr-312
    "371-373-375-377-378-379-381-382-383-384-385-386-387-388-390-391-392-395-399-400-403-404-405-408-411-413-415-416-419-420-424-435-436-441-442-443-444",  # lead pr-371
    "431-450-452-455-456-457-463-467-472-473-474-475-476-477-480-481-485-486-487-490-491-492-493-494-495-496-497-499-500-501-502-503-505-506-507-508-512-516-517-528-530-534-536-540-548-549-550-551-556-562-563-564-565-569",  # lead pr-431
    "461-462-464-466",  # lead pr-461
    "543-585-587-588-591-592-594-597-598-599-600-603-605-606-610-612-613-614-616-617-618-620-621-622-624-625-627-628-631-633-636-642-644-647-649-650",  # lead pr-543
    "653-683-684-686-687",  # lead pr-653
    "660-662-663-665-666-668-670-671-672-673-676-677",  # lead pr-660
    "688-689-696-697-698-702-703-704-705-708-710-713-714-715-716-717",  # lead pr-688
]

for _ni in _BUNDLE_NIS_Cattrs:
    Instance.register("python-attrs", _ni)(Cattrs)
