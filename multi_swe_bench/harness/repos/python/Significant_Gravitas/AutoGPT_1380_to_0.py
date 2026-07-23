import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _target_test_files(patch: str, strip_prefix: str = "") -> list[str]:
    """Return only the real test files a PR's gold test_patch targets.

    Uses the target side (``+++ b/<path>``) so newly-ADDED test files are
    included (unlike test_result.get_modified_files, which keeps only source
    files and so drops brand-new test modules). Filters to genuine test files
    (``tests.py`` / ``test_*.py`` / ``*_test.py`` / ``*_tests.py`` under a
    ``tests``/``test`` dir), excluding fixtures/support (conftest.py, __init__.py,
    utils.py) and source files that merely match the name (e.g.
    autogpt/commands/write_tests.py, which is NOT under a tests dir). Running
    only these isolates the PR's F2P signal from unrelated flaky live/API tests.
    """
    out: list[str] = []
    seen: set[str] = set()
    for f in re.findall(r"^\+\+\+ b/(.+?)\s*$", patch, re.M):
        if f == "/dev/null":
            continue
        base = f.rsplit("/", 1)[-1]
        if not base.endswith(".py"):
            continue
        if base in ("conftest.py", "__init__.py", "utils.py"):
            continue
        is_test = (
            base == "tests.py"
            or base.startswith("test_")
            or base.endswith("_test.py")
            or base.endswith("_tests.py")
        )
        under_tests = any(s in ("tests", "test") for s in f.split("/")[:-1])
        if base == "tests.py" or (is_test and under_tests):
            p = f[len(strip_prefix):] if strip_prefix and f.startswith(strip_prefix) else f
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


# Only each PR's own gold test files are run (see _target_test_files). PYTHONPATH
# carries the repo root plus the era-0 `scripts/` and post-rename `autogpt/`
# dirs so the era's ancient CWD-relative imports (e.g. a test doing
# `sys.path.append("../scripts"); from promptgenerator import ...`) resolve while
# pytest still runs from /home/AutoGPT (so parse_log node names stay "tests/...").
_ERA0_PYTHONPATH = "/home/AutoGPT:/home/AutoGPT/scripts:/home/AutoGPT/autogpt"

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA0_PYTHONPATH + """
cd /home/AutoGPT
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files present at baseline stage"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_APPLY_FN = """apply_patch_lenient() {
    local p="$1"
    if git -C /home/AutoGPT apply --whitespace=nowarn \\
        --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' \\
        --exclude='*.ico' --exclude='*.zip' --exclude='*.pdf' --exclude='*.bin' \\
        --exclude='*.wasm' --exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' \\
        --exclude='*.otf' --exclude='*.eot' --exclude='*.tar.gz' --exclude='*.so' \\
        --exclude='*.dylib' "$p" 2>/dev/null; then
        return 0
    fi
    echo "git apply failed for $p, trying patch -p1 --fuzz=3 fallback..." >&2
    if patch -p1 --forward --fuzz=3 --batch --reject-file=/tmp/$(basename $p).rej < "$p" 2>&1 | tail -50; then
        return 0
    fi
    echo "Warning: $p did not fully apply, continuing anyway" >&2
    return 0
}
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA0_PYTHONPATH + """
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after test.patch"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
export PYTHONPATH=""" + _ERA0_PYTHONPATH + """
cd /home/AutoGPT
""" + _APPLY_FN + """apply_patch_lenient /home/test.patch
apply_patch_lenient /home/fix.patch
TEST_FILES="@@TEST_FILES@@"
RUN=""
for f in $TEST_FILES; do [ -f "$f" ] && RUN="$RUN $f"; done
if [ -z "$RUN" ]; then echo "No target test files after patches"; exit 0; fi
python -m pytest $RUN -v --tb=short --continue-on-collection-errors -p no:cacheprovider \\
    -o python_files='test_*.py *_test.py *_tests.py'
"""


# PRs 215, 463, 754, 1380 (April 2023, Auto-GPT release 0.1 -> 0.2)
# Pre-pyproject.toml era. Layout: scripts/ (or autogpt/ after PR 1380's rename),
# requirements.txt at repo root, tests/ directory contains a mix of unittest-
# and pytest-style tests. Python 3.11 works in practice (Dockerfile of this era
# pins python:3.11; the 3.8 matrix in CI is too old for pyyaml==6.0 wheels under
# modern toolchains).
#
# Two-class layout: ImageBase holds the system-level apt deps + repo clone and
# is SHARED across all PRs in this era (so the harness builds the base image
# once, then each PR's ImageDefault layers patches + scripts on top).


class ImageBase(Image):
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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        # Fixed per-era tag so all PRs in this era share one cached base image.
        return "base-era0"

    def workdir(self) -> str:
        return "base-era0"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base = self.dependency()
        if isinstance(base, Image):
            base = base.image_full_name()
        # Minimal base: apt deps only. Repo clone is deferred to prepare.sh
        # to keep the cross-arch base build small (the AutoGPT repo at HEAD is
        # ~3,800 files and clone+layer-export dominates buildkit runtime,
        # which caused the parallel native-arch rebuild to OOM out previously).
        #
        # The leading `# syntax=docker/dockerfile:1.6` directive makes
        # DockerfileEnhancer.enhance() emit this Dockerfile verbatim: it injects
        # the ARG/ENV/LABEL + proxy/CA-cert infra ONLY when that directive is
        # absent. This is how the base opts out of proxy and certificate
        # injection without modifying image.py.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base}

ARG TARGETARCH
ARG REPO_URL="https://github.com/Significant-Gravitas/AutoGPT.git"
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV TZ=UTC
ENV LANG=C.UTF-8
LABEL org.opencontainers.image.title="Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.description="Significant-Gravitas/AutoGPT base image (era0)" \\
      org.opencontainers.image.source="github.com/Significant-Gravitas/AutoGPT" \\
      org.opencontainers.image.authors="ethara.ai"

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip 'setuptools<81' wheel

# Clone at HEAD (no base commit) and pre-install the COMMON deps so per-PR
# images reuse this layer. The authoritative per-commit checkout + install runs
# later in prepare.sh; the `|| true` keeps the base build green even when HEAD's
# layout/requirements drift from the era's pinned commits.
RUN git clone "${{REPO_URL}}" /home/AutoGPT

WORKDIR /home/AutoGPT

RUN pip install --no-cache-dir -r requirements.txt || true

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}-era0"

    def workdir(self) -> str:
        # Instance-dir name. gen_report.collect_report_tasks parses the PR number
        # via int(dir_name[3:]) expecting "pr-<number>", so NO era suffix here
        # (an "-era0" suffix makes int("<n>-era0") raise and the instance is
        # silently skipped -> 0 reports). PR numbers are unique across eras, so
        # "pr-<number>" cannot collide. The era stays on image_tag() above so the
        # per-era images remain distinct/cached.
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
cd /home
BASE_COMMIT="{pr.base.sha}"
# Clone here (deferred from ImageBase to keep the shared base image small).
# If the layer cache happens to contain /home/AutoGPT from a sibling build,
# just reuse it; the reset+checkout below pins to the per-PR sha either way.
if [ ! -d /home/AutoGPT/.git ]; then
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
fi
cd /home/AutoGPT
git reset --hard 2>/dev/null || true
# A sibling build may have reused + hardened (history-pruned) this checkout to a
# different base commit; if the sha we need is gone, re-clone before checkout.
if ! git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null; then
    cd /home && rm -rf /home/AutoGPT
    git clone https://github.com/Significant-Gravitas/AutoGPT.git /home/AutoGPT
    cd /home/AutoGPT
fi
git checkout "${{BASE_COMMIT}}"
# --- Reward-hacking hardening (mirrors image.Image._HARDENING_BLOCK) ---------
# Detach at the base commit and strip every ref, remote, reflog, tag and
# alternate so the evaluated model cannot recover the fix from git history or
# the PR branch. Assertions abort the build (set -e) if anything leaks through.
git checkout --detach "${{BASE_COMMIT}}"
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi
# --- end hardening -----------------------------------------------------------
if ! pip install --no-cache-dir -r requirements.txt; then
    while IFS= read -r line; do
        clean=$(echo "$line" | sed 's/#.*//' | xargs)
        [ -z "$clean" ] && continue
        pip install --no-cache-dir "$clean" || true
    done < requirements.txt
fi
pip install --no-cache-dir pytest pytest-mock pytest-asyncio sentry-sdk || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                _RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                _TEST_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _FIX_RUN_SH.replace(
                    "@@TEST_FILES@@",
                    " ".join(_target_test_files(self.pr.test_patch)),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_name()
        base_tag = base.image_tag()
        # COPY prepare.sh and RUN it FIRST so the expensive clone+install layer
        # caches independently of the run/test/fix scripts + patches. Editing those
        # then only invalidates the cheap trailing COPYs, not the pip/poetry install.
        other = [f for f in self.files() if f.name != "prepare.sh"]
        copy_rest = "\n".join(f"COPY {f.name} /home/" for f in other)
        return f"""FROM {base_name}:{base_tag}

COPY prepare.sh /home/

RUN bash /home/prepare.sh

{copy_rest}
"""


@Instance.register("Significant-Gravitas", "AutoGPT_1380_to_0")
class AUTOGPT_1380_TO_0(Instance):
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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest verbose lines: "tests/path/test_file.py::test_name PASSED"
        pattern = r"(\btests(?:\.py|/[^\s]*?)::[^\s]+?) (PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b"
        for test_name, status in re.findall(pattern, log):
            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(test_name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# === bundle number_interval routing (prs_in_bundle dash-joined) ===
# Each dataset record carries number_interval = its prs_in_bundle joined by
# "-" (NOT a low-high range). Instance.create() routes on
# f"{org}/{number_interval}" whenever a record sets number_interval, so every
# bundle interval belonging to this era must be registered to AUTOGPT_1380_TO_0
# (in addition to the "AutoGPT_1380_to_0" name on the class above), else create() raises
# "Instance ... is not registered" before any image is built.
_BUNDLE_NUMBER_INTERVALS = [
    "1380-1393-1397-1418-1426-1432-1444-1452-1478",
    "463-742-780-781-802-810-938-965-1011-1014-1022-1028-1031-1032-1033-1038-1044-1050-1053-1062-1065-1068-1071-1072-1087-1095-1121-1147-1148-1151-1155-1156",
    "754-774-836-837-884-968-970-980-992-1016-1034-1096-1118-1120-1125-1138-1142-1144-1158-1197-1220-1231-1232-1236-1242-1312-1323-1347-1365",
    "215-685-697-700-794-798-827-865-913-923-957-981-1002-1007-1017",
]
for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("Significant-Gravitas", _ni)(AUTOGPT_1380_TO_0)
