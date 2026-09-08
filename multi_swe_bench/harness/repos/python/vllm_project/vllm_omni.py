"""vllm-project/vllm-omni config (pytest, CPU-only vLLM runtime).

One shared base image for the whole repo, one image per PR -- the structure of
``typescript/lobehub/lobehub_6452_to_71.py``.

Not era-scoped and not PR-scoped: no commit sha, PR number, test path or vLLM
version is written into this file. Everything PR-specific is read at build time,
either off the ``PullRequest`` record (``base.sha``, ``repo``, the patches) or out
of the repository tree once it is checked out at that commit. The PR numbers named
in the notes below are observations about the dataset this was written against,
not values the config depends on.

Environment notes specific to this repo:

* ``vllm`` is deliberately NOT a declared dependency of vllm-omni (pyproject
  carries ``# "vllm==0.11.0",  # TODO: fix the entrypoints overwrite problem``),
  so it must be installed separately. The version to install is neither guessed
  nor pinned here: ``docker/Dockerfile.ci`` declares ``ARG VLLM_BASE_TAG`` at
  every commit, so ``prepare.sh`` reads it out of the working tree once it is
  checked out at ``BASE_COMMIT``. Nothing about a specific commit, PR number or
  release is baked into this file, so a regenerated dataset covering different
  PRs installs the right vLLM without an edit here.
* From PR ~1394 the repo grew a ``setup.py`` that routes dependencies by detected
  hardware. Under pip build isolation ``import torch`` fails inside the build env
  and it falls back to ``requirements/cuda.txt`` (``fa3-fwd``, CUDA cublas pins).
  ``VLLM_OMNI_TARGET_DEVICE=cpu`` is priority 1 in ``detect_target_device()`` and
  short-circuits that, so ``prepare.sh`` exports it.
* That same ``setup.py`` derives its version through ``setuptools_scm``.
  ``VLLM_OMNI_VERSION_OVERRIDE`` short-circuits the git-tag lookup, which matters
  because the per-PR hardening layer deletes every tag right after the install.
* The install is EDITABLE on purpose: several fix patches add brand-new modules
  (e.g. PR 292 adds ``vllm_omni/entrypoints/openai/image_api_utils.py`` and the
  ``protocol/`` subpackage). A non-editable install would not see them when
  ``fix-run.sh`` applies the patch after the image is built.

Test scoping: the ``tests/`` tree contains GPU e2e suites that cannot run here, so
the three run scripts execute only the test files the gold ``test_patch`` touches.
``run.sh`` runs before that patch is applied and therefore filters the list down to
files that already exist at ``base.sha`` -- a file the patch creates simply yields
no baseline result, which is the correct ``NONE`` baseline.

Known-unrunnable instance: PR 168 adds ``tests/multi_stages/test_qwen_omni.py``, a
Qwen2.5-Omni end-to-end test that needs a GPU and model weights. It is kept in
scope (its ``core_model`` marker registration is a genuine fix) but it will not
produce a fail-to-pass transition on CPU.
"""

import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# pyproject sets python_files = ["test_*.py", "*_test.py"]; mirror it so helper
# modules the test patch also adds (conftest.py, utils.py, __init__.py, yaml
# fixtures) are not handed to pytest as targets.
_TEST_FILE_RE = re.compile(r"^(?:test_.+|.+_test)\.py$")

# pytest -v inline results and the ``-rA`` short summary. Both are anchored on a
# leading whitespace-free path token containing "::", so neither vLLM's own log
# output nor a traceback line can be mistaken for a test result.
_INLINE_RE = re.compile(
    r"^(?P<name>\S+::\S*(?:[ \t]\S+)*?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
    r"(?:\s+\[\s*\d+%\s*\])?\s*$"
)
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
    r"(?P<name>\S+::\S+?)(?:\s+-\s.*)?$"
)


def _test_targets(test_patch: str) -> str:
    """Space-joined test files the gold test patch adds or modifies.

    Reads the ``+++ b/`` side so newly added files are included (``get_modified_files``
    reads the ``a/`` side and drops them). Falls back to the whole ``tests/`` tree
    only if a patch carries no recognisable test file at all.
    """
    targets: list[str] = []
    for path in re.findall(r"^\+\+\+ b/(.+)$", test_patch, re.MULTILINE):
        path = path.strip()
        if not path.startswith("tests/"):
            continue
        if not _TEST_FILE_RE.match(path.rsplit("/", 1)[-1]):
            continue
        if path not in targets:
            targets.append(path)
    return " ".join(sorted(targets)) if targets else "tests/"


# Exported by every run script. VLLM_TARGET_DEVICE / VLLM_WORKER_MULTIPROC_METHOD
# match what tests/conftest.py sets for itself; the rest keep vLLM off the network
# and out of any GPU probe. --no-cov neutralises the --cov* addopts the early era
# pins in pyproject without dropping --strict-markers, which is the very thing
# PR 168's fix patch turns from a collection error into a pass.
_RUN_ENV = """\
export CI=true
export PYTHONUNBUFFERED=1
export PYTHONPATH=/home/{repo}:${{PYTHONPATH}}
export CUDA_VISIBLE_DEVICES=""
export VLLM_TARGET_DEVICE=cpu
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export VLLM_LOGGING_LEVEL=ERROR
export VLLM_NO_USAGE_STATS=1
export DO_NOT_TRACK=1
export HF_HUB_DISABLE_TELEMETRY=1
export TOKENIZERS_PARALLELISM=false
"""

_PYTEST_CMD = "python -m pytest -v -rA --no-cov -p no:cacheprovider"


class VllmOmniImageBase(Image):
    """Single shared base image for every vllm-omni PR."""

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
        # bookworm, not slim: several runtime deps (sox, openai-whisper,
        # mooncake-transfer-engine) build from source on install. 3.12 is inside
        # requires-python for every era here (>=3.9,<3.14 early, >=3.10,<3.14 late)
        # and inside the supported range of vLLM 0.11 through 0.16.
        return "python:3.12-bookworm"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # Shared base for every PR: clones the repo ONCE and installs the system
        # libraries the audio/video test dependencies link against. It deliberately
        # does NOT check out ``${BASE_COMMIT}`` and does NOT prune git history -- a
        # single ``base`` tag is shared by all five PRs but each has a different
        # ``base.sha``, so pinning here would strip every other PR's commit out of
        # history. The per-PR checkout and hardening happen in the per-PR image.
        #
        # Two DockerfileEnhancer interactions (image.py) are load-bearing here,
        # because this image's dependency() is a str and so it IS processed:
        #   * ``_standardize_repo_fetch`` rewrites a hardcoded ``git clone <url>``
        #     into a BASE_COMMIT-pinned sequence. Its Pattern-2 regex carries a
        #     negative lookahead on the literal ``"${REPO_URL}"``, so writing the
        #     clone against that literal (injected as an ARG by the infra block)
        #     leaves it untouched.
        #   * ``_inject_final_sanitize`` appends a BASE_COMMIT-pinned hardening
        #     block to any Dockerfile that mentions ``git clone`` -- unless the
        #     content already carries the hardening marker line before its CMD.
        #     The comment below supplies that marker, so the shared base stays
        #     unpinned. It must sit AFTER the clone: the enhancer re-injects if a
        #     clone/fetch appears between the marker and the CMD.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    ffmpeg \\
    sox \\
    libsox-fmt-all \\
    libsndfile1 \\
    libgl1 \\
    libglib2.0-0 \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel setuptools-scm

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class VllmOmniImageDefault(Image):
    """PR-specific image for vllm-omni."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return VllmOmniImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets = _test_targets(self.pr.test_patch)
        env = _RUN_ENV.format(repo=self.pr.repo)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_ROOT_USER_ACTION=ignore
# setup.py (late era) routes install_requires by detected hardware and falls back
# to requirements/cuda.txt when torch is absent from the isolated build env.
export VLLM_OMNI_TARGET_DEVICE=cpu
export VLLM_TARGET_DEVICE=cpu
# Short-circuits the setuptools_scm git-tag lookup; the hardening layer that runs
# straight after this script deletes every tag.
export VLLM_OMNI_VERSION_OVERRIDE=0.0.0.dev0
export SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0

# vLLM is not a declared dependency of vllm-omni. Read the release this very
# commit builds its CI image from out of the checked-out tree, so no PR number,
# commit or version is hardcoded in the config. If the file is absent or does not
# declare the tag, fall back to an unpinned install rather than a stale guess.
VLLM_SPEC="vllm"
if [ -f docker/Dockerfile.ci ]; then
    VLLM_TAG=$(tr -d '\\r' < docker/Dockerfile.ci \\
        | sed -n 's/^ARG VLLM_BASE_TAG=v*\\([0-9][0-9.]*\\).*$/\\1/p' | head -n 1)
    if [ -n "$VLLM_TAG" ]; then
        VLLM_SPEC="vllm==$VLLM_TAG"
    fi
fi

echo "prepare.sh: installing $VLLM_SPEC (from docker/Dockerfile.ci at this commit)"
python -m pip install --no-cache-dir "$VLLM_SPEC" || true

# Editable: fix patches applied after build time add new modules under vllm_omni/.
python -m pip install --no-cache-dir -e ".[dev]" || true

# Guarantee the runner and the plugins pyproject addopts/asyncio_mode require,
# even if the editable install above degraded.
python -m pip install --no-cache-dir \\
    pytest pytest-asyncio pytest-cov pytest-mock || true
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash

{env}
cd /home/{repo}

# Baseline: this runs before test.patch, so a target the patch creates does not
# exist yet. Filter to what is present rather than letting pytest exit on a bad
# path, so an already-existing test still reports its pre-patch status.
TARGETS=""
for f in {targets}; do
    if [ -e "$f" ]; then
        TARGETS="$TARGETS $f"
    fi
done

if [ -z "$TARGETS" ]; then
    echo "run.sh: no gold test target exists at the base commit; nothing to run"
    exit 0
fi

{pytest} $TARGETS 2>&1 || true
""".format(env=env, repo=self.pr.repo, targets=targets, pytest=_PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -e

{env}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch

set +e
{pytest} {targets} 2>&1 || true
""".format(env=env, repo=self.pr.repo, targets=targets, pytest=_PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -e

{env}
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

set +e
{pytest} {targets} 2>&1 || true
""".format(env=env, repo=self.pr.repo, targets=targets, pytest=_PYTEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("ImageDefault dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The repo is cloned once in the shared base image, so this layer does
        # NOT clone (and needs no ``REPO_URL``): it only checks out this PR's
        # commit in the inherited working tree.
        #
        # This per-PR image chains to a base *Image* (not a string), so
        # DockerfileEnhancer returns this dockerfile verbatim and does NOT
        # auto-inject git-history hardening. We therefore check out
        # ``${BASE_COMMIT}`` and apply ``Image._HARDENING_BLOCK`` manually so the
        # fix / future commits cannot be read out of git history (reward hacking).
        # ``BASE_COMMIT`` is pinned to *this* PR's ``base.sha``, which also prunes
        # the full history inherited from the shared base.
        #
        # Ordering matters: prepare.sh installs while the git tags are still
        # intact (setuptools_scm), and the hardening block runs immediately after.
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("vllm-project", "vllm-omni")
class VLLM_OMNI(Instance):
    """Instance for vllm-project/vllm-omni.

    The raw dataset carries neither ``tag`` nor ``number_interval``, so
    ``Instance.create`` resolves the registry name to ``f"{org}/{repo}"`` ->
    ``"vllm-project/vllm-omni"``. Registration therefore uses the raw hyphenated
    names, not the underscored package directory they live in.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return VllmOmniImageDefault(self.pr, self._config)

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

        buckets = {
            "PASSED": passed_tests,
            "XPASS": passed_tests,
            "FAILED": failed_tests,
            "ERROR": failed_tests,
            "SKIPPED": skipped_tests,
            "XFAIL": skipped_tests,
        }

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        for line in clean_log.splitlines():
            stripped = line.strip()
            if "::" not in stripped:
                continue

            m = _INLINE_RE.match(stripped) or _SUMMARY_RE.match(stripped)
            if m:
                buckets[m.group("status")].add(m.group("name").strip())

        # Dedup: worst wins. TestResult.__post_init__ rejects any overlap.
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
