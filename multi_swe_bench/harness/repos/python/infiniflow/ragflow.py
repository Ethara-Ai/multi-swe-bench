import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# NOTE ON COMMENTS: everything explained here lives in this generator, never in
# the artifacts it emits. The rendered Dockerfiles and shell scripts are kept
# comment-free by design.

# RAGFlow's suite splits three ways: `test/unit_test` (pure unit tests), a few
# `*_unit.py` files under `test/testcases` that stub every service they touch,
# and the rest of `test/testcases`, which only passes against a live deployment
# (API server + doc engine + MinIO + Redis + MySQL). The three run scripts
# therefore execute `test/unit_test` -- present and green at all five base
# commits -- plus whatever the PR's own test patch touches. The directory is the
# constant baseline, so every stage including `run` reports real numbers instead
# of (0, 0, 0), and pass-to-pass is meaningful rather than empty.
_BASELINE_TEST_DIR = "test/unit_test"

# Which commit seeds the one shared base image.
#
# All five instances produce a RagflowImageBase with the same full name and the
# pipeline collects them into a set, so exactly one survives and its
# pr.base.sha becomes the BASE_COMMIT build-arg. Left to chance that is the
# first line of the JSONL -- the oldest PR -- and every newer PR then has to
# fetch its own commit inside prepare.sh, because the base image's history scrub
# pruned everything outside the seed commit's ancestry.
#
# Seeding from the newest PR removes the fetch entirely: these base commits are
# all on `main` and strictly ordered, so every older one is an ancestor of the
# newest and is already in the clone. Verified with `git merge-base
# --is-ancestor` for all four older SHAs against e705ac66; PR number order
# matches commit order exactly (12546 Jan 14 -> 13197 Feb 24 -> 13650 Mar 17 ->
# 13709 Mar 19 -> 13784 Mar 26).
#
# Every instance registers itself on construction and the base image reports the
# highest-numbered PR, so the outcome no longer depends on which object won the
# set. PR number is the ordering key: it is the only monotonic-in-time field on
# PullRequest.
_BASE_SEEDS: dict[str, PullRequest] = {}


def _register_base_seed(pr: PullRequest) -> None:
    key = f"{pr.org}/{pr.repo}"
    current = _BASE_SEEDS.get(key)
    if current is None or pr.number > current.number:
        _BASE_SEEDS[key] = pr


def _base_seed(pr: PullRequest) -> PullRequest:
    return _BASE_SEEDS.get(f"{pr.org}/{pr.repo}", pr)


_TEST_FILE_RE = re.compile(r"^\+\+\+ b/(\S+)", re.MULTILINE)


def _test_files(test_patch: str) -> list[str]:
    """The pytest files a test patch adds or modifies, in patch order.

    Only ``test_*.py`` is returned: a test patch may also carry ``conftest.py``
    or ``__init__.py`` (PR 13709 adds both), which must be applied but must
    never be handed to pytest as a target.
    """
    files: list[str] = []
    for match in _TEST_FILE_RE.finditer(test_patch):
        path = match.group(1)
        name = path.rsplit("/", 1)[-1]
        if name.startswith("test_") and name.endswith(".py"):
            files.append(path)
    return list(dict.fromkeys(files))


_PROD_PKG_ROOTS = ("api/", "rag/", "common/", "deepdoc/", "agent/", "graphrag/")


def _fix_patch_new_modules(fix_patch: str) -> list[str]:
    """Dotted names of production modules the fix patch *adds*.

    The `*_unit.py` tests under test/testcases load a route module by file path
    after stubbing its dependencies into sys.modules. When a fix patch adds a new
    production module, those stubs are frozen at the pre-fix API: PR 13784's
    test stubs `common.constants` with only `RetCode`, while the fix's new
    `api/utils/tenant_utils.py` does `from common.constants import LLMType`. The
    import blows up and takes all 12 tests in the file with it -- a 12-test
    pass-to-fail that has nothing to do with the behaviour under test, and which
    upstream shares (the merged test file at ff92b557 has the same stub).

    Importing such a module *before* pytest starts binds it against the real
    `common.constants`, so the partial stub never gets the chance to break it.
    The import is best-effort: in the baseline stage the module does not exist
    yet, which keeps the graded command byte-identical across all three stages.

    Modified files count, not just created ones: PR 13784's tenant_utils.py
    already exists at the base commit (mode 100644) and the fix merely *adds* the
    LLMType import to it, which is exactly what the stale stub cannot satisfy.

    Each module is imported under its own guard. A single shared try/except would
    let the first failure -- api/apps/file2document_app.py needs a Quart app
    context and raises on import -- silently skip every module after it, which is
    what made the first version of this a no-op.
    """
    modules: list[str] = []
    for match in _TEST_FILE_RE.finditer(fix_patch):
        path = match.group(1)
        if not path.endswith(".py") or not path.startswith(_PROD_PKG_ROOTS):
            continue
        modules.append(path[: -len(".py")].replace("/", "."))
    return list(dict.fromkeys(modules))


def _pytest_targets(test_patch: str) -> list[str]:
    """Baseline directory first, then any patch target outside it.

    A target already inside ``test/unit_test`` (PRs 13197, 13650, 13709) is
    dropped -- the directory run collects it, and passing it twice would execute
    the same node ids in two processes for nothing. PRs 12546 and 13784 touch
    ``test/unit`` and ``test/testcases``, which the baseline does not cover.
    """
    prefix = _BASELINE_TEST_DIR + "/"
    extra = [p for p in _test_files(test_patch) if not p.startswith(prefix)]
    return [_BASELINE_TEST_DIR] + extra


# RAGFlow moved graspologic from github.com/yuzhichang to gitee.com/infiniflow
# partway through this commit range; the newest lock (which now seeds the base)
# pins the Gitee URL, and Gitee refuses anonymous git fetches from CI networks
# ("fatal: could not read Username"). The GitHub mirror carries the identical
# commit 38e680cab72bc9fb68a7992c3bcc2d53b24e42fd, so rewriting that one URL
# makes both lockfile variants install byte-identical source.
_GRASPOLOGIC_REDIRECT = (
    'git config --global '
    'url."https://github.com/yuzhichang/graspologic.git".insteadOf '
    '"https://gitee.com/infiniflow/graspologic.git"'
)

# deepdoc's parsers call nltk.word_tokenize. RAGFlow's own Dockerfile bakes
# nltk_data in from the infiniflow/ragflow_deps image, which is unreachable
# here; without these corpora 9 tests in test/unit_test/deepdoc fail on
# "Resource 'punkt_tab' not found".
_NLTK_DOWNLOAD = (
    "python -m nltk.downloader -d /usr/share/nltk_data "
    "punkt punkt_tab wordnet stopwords"
)

# common/token_utils.py imports tiktoken, which downloads cl100k_base from
# openaipublic.blob.core.windows.net at *import* time. Any test module that
# reaches token_utils therefore depends on live DNS: one failed lookup produced
# 8 collection errors and 12 failures in a single stage, swinging the pass count
# by 119 and looking exactly like a patch-induced regression. Warming the cache
# at build time under a fixed TIKTOKEN_CACHE_DIR makes the import offline-safe;
# verified by monkeypatching socket.getaddrinfo to raise and re-importing.
# RAGFlow's own Dockerfile bakes in the same file
# (9b5ad71b2ce5302211f9c61530b329a4922fc6a4, the sha1 of the download URL).
_TIKTOKEN_WARM = "python -c \"import tiktoken; tiktoken.get_encoding('cl100k_base')\""

# Each target gets its own pytest process. `test/testcases/conftest.py` installs
# a session-scoped autouse fixture that calls pytest.exit() when the RAGFlow
# server is unreachable, which would tear down the whole session including the
# unrelated unit tests. Isolating targets also keeps the two conflicting
# `common` modules apart: `test/unit_test` imports the repository's `common`
# package while `test/testcases`' conftest imports its own sibling `common.py`,
# and only one can own that name per process.
#
# --continue-on-collection-errors is load-bearing, not cosmetic. A feature PR's
# new test module imports a symbol that does not exist until the fix patch lands
# (PR 13709's `from rag.llm.embedding_model import PerplexityEmbed`), and pytest
# treats a collection ImportError as fatal: it aborts with "Interrupted: 1 error
# during collection" and reports nothing at all, which is what reduced the test
# stage to (0, 0, 0). With the flag the same command yields "576 passed, 25
# skipped, 1 error" and the module still transitions to passed under the fix.
_RUN_TESTS_FUNC = """run_pytest() {{
    local target
    local extra
    local status=0

    for target in {targets}; do
        if [ ! -e "$target" ]; then
            echo "SKIP: $target does not exist at this commit"
            continue
        fi
        extra=""
        case "$target" in
            test/testcases/*) extra="--noconftest -p rf_preimport" ;;
        esac
        echo "===== pytest $target ====="
        python -m pytest -v -rA --color=no -p no:cacheprovider \\
            --continue-on-collection-errors $extra "$target" || status=1
    done

    return $status
}}
"""

# TZ: RAGFlow's time helpers assume UTC+8. Under the TZ=UTC the pipeline bakes
# in, 11 tests in test/unit_test/common/test_time_utils.py fail on an 8-hour
# offset (assert 1704067200000 == 1704038400000).
#
# ZHIPU_AI_API_KEY: test/testcases/configs.py calls pytest.exit() at import time
# when it is unset, aborting before collection -- the reason PR 13784 reported
# (0, 0, 0) in all three stages. It is only read by fixtures that register a
# model against a live server, which the stubbed *_unit.py tests never reach.
#
# PYTHONPATH carries only the repo root; pyproject's `pythonpath = ["."]` covers
# it for pytest, but the export keeps plain `python -c` invocations consistent.
# sdk/python is deliberately absent: ragflow_sdk's __init__ calls
# importlib.metadata.version(), which needs real distribution metadata that a
# sys.path entry cannot provide, so prepare.sh installs it instead.
_RUN_ENV = """export CI=true
export TZ=Asia/Shanghai
export ZHIPU_AI_API_KEY="${{ZHIPU_AI_API_KEY:-placeholder-for-offline-unit-tests}}"
export HOST_ADDRESS="${{HOST_ADDRESS:-http://127.0.0.1:9380}}"
export PYTHONPATH="/home:/home/{repo}\""""


class RagflowImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        # Deliberately not self._pr: the base image is shared, so it must
        # describe the seed commit rather than whichever instance constructed
        # it. org/repo are identical across instances, so only base.sha -- the
        # BASE_COMMIT build-arg -- actually changes.
        return _base_seed(self._pr)

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        # pyproject.toml pins requires-python = ">=3.12,<3.15" and CI runs
        # `uv sync --python 3.12`, so 3.12 is the interpreter the lockfile was
        # resolved against. Non-slim already ships git and a toolchain.
        return "python:3.12"

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

        # Written as `git clone "${REPO_URL}"` on purpose. DockerfileEnhancer
        # rewrites a clone line into its own clone/checkout/scrub block ending in
        # CMD -- but its pattern carries a negative lookahead for exactly this
        # form, so spelling it this way leaves the line alone and the enhancer
        # only injects the history scrub ahead of the CMD below. That is what
        # lets the dependency install sit *after* the checkout, operating on the
        # real working tree, instead of needing a throwaway clone to fish
        # pyproject.toml and uv.lock out of the remote before the repo exists.
        # (The `need_clone=False` COPY form is not offered: the enhancer rewrites
        # COPY into a clone regardless, so it was never actually honoured.)
        code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'

        # The apt list carries only what python:3.12 lacks -- `dpkg -s` in the
        # image confirms curl, gnupg, libicu-dev, libxext6, libxrender1, make,
        # unzip and wget are already present. git and ca-certificates stay
        # explicit because they are the hard floor for cloning over TLS.
        # build-essential and pkg-config are needed because uv.lock still builds
        # a few sdists from source (datrie, aliyun-python-sdk-core); libgl1 and
        # libglib2.0-0 are linked by opencv-python at import; ghostscript is
        # shelled out to by api/utils/file_utils.py when repairing a PDF.
        #
        # Everything else is one RUN. The dependency install must precede the
        # clone because the pipeline's DockerfileEnhancer rewrites the clone line
        # into the standard clone/checkout/hardening block terminated by
        # CMD ["/bin/bash"], so anything after it would land past the CMD. The
        # manifests come from a throwaway depth-1 fetch of ${REPO_URL} (~20s)
        # rather than a hard-coded raw.githubusercontent.com URL, so overriding
        # REPO_URL redirects the manifest fetch and the clone alike.
        # `uv cache prune --ci` then drops the downloadable wheels that
        # UV_LINK_MODE=copy already duplicated into the venv: 3.7 GB of cache
        # down to 86 MB, with the from-source builds kept.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV TZ=Asia/Shanghai \\
    NLTK_DATA=/usr/share/nltk_data \\
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    UV_HTTP_TIMEOUT=600 \\
    UV_LINK_MODE=copy \\
    UV_CACHE_DIR=/opt/uv-cache \\
    UV_PROJECT_ENVIRONMENT=/opt/ragflow-venv \\
    VIRTUAL_ENV=/opt/ragflow-venv \\
    PATH=/opt/ragflow-venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    ghostscript \\
    git \\
    libgl1 \\
    libglib2.0-0 \\
    pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip \\
    && python -m pip install --no-cache-dir "uv>=0.5"

{code}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN {_GRASPOLOGIC_REDIRECT} \\
    && uv sync --python 3.12 --group test --frozen --no-install-project \\
    && uv cache prune --ci \\
    && {_NLTK_DOWNLOAD} \\
    && {_TIKTOKEN_WARM}

{self.clear_env}

CMD ["/bin/bash"]
"""


class RagflowImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return RagflowImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        targets = " ".join(f'"{path}"' for path in _pytest_targets(self.pr.test_patch))
        run_pytest = _RUN_TESTS_FUNC.format(targets=targets)
        run_env = _RUN_ENV.format(repo=self.pr.repo)
        preimport = "".join(
            f"{mod!r}, " for mod in _fix_patch_new_modules(self.pr.fix_patch)
        )

        # prepare.sh shares no command with the base image, by explicit request.
        #
        # No `git fetch`: the base is seeded from the newest PR, so every base
        # commit is already in the clone and a bad seed fails loudly at
        # `git checkout` under `set -e`.
        #
        # No `git reset --hard` either, even though the reference configs open
        # with one. The base image asserts a clean tree at build time, so the
        # reset is a no-op; dropping it lets the first check_git_changes.sh
        # actually verify what the base handed over instead of asserting against
        # a reset this script just performed.
        #
        # No per-PR `uv sync` either. The base venv is resolved from the newest
        # PR's lockfile, and running `test/unit_test` at each of the other four
        # base commits against that venv reproduces the per-PR-sync counts
        # exactly (240 / 327 / 561 / 576 passed, 25 skipped). The re-sync
        # therefore changed no result while rewriting ~2.5 GB of venv into every
        # PR layer -- which is what pushed the five images past 10 GB each and
        # made a 5-minute layer commit take 11 hours on a full disk. Only the
        # editable SDK install remains: test/testcases/test_web_api's conftest
        # imports ragflow_sdk, whose __init__ calls importlib.metadata.version(),
        # which needs real distribution metadata that a sys.path entry cannot
        # supply.
        #
        # KNOWN CONSEQUENCE -- the history re-scrub was removed on request.
        # Seeding from the newest PR means `git checkout <older sha>` leaves the
        # seed commit and everything between it and here in .git: unreachable,
        # but readable with `git cat-file`. For PR 12546 (base Jan 14, fix merged
        # Jan 15) the seed tree e705ac66 contains that PR's own merged fix, so an
        # agent can recover the answer from the object store. Closing it needs
        # `git reflog expire --expire=now --all` plus `git gc --prune=now`, which
        # duplicate two base-image commands; oldest-first seeding plus a depth-1
        # fetch avoids both the duplication and the leak.
        return [
            File(
                ".",
                "rf_preimport.py",
                """for _name in ({preimport}):
    try:
        __import__(_name)
    except Exception:
        pass

""".format(preimport=preimport),
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

uv pip install -e sdk/python

python --version
python -m pytest --version

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -uo pipefail

{run_env}

cd /home/{pr.repo}

{run_pytest}
run_pytest

""".format(pr=self.pr, run_pytest=run_pytest, run_env=run_env),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -uo pipefail

{run_env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi

{run_pytest}
run_pytest

""".format(pr=self.pr, run_pytest=run_pytest, run_env=run_env),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -uo pipefail

{run_env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{run_pytest}
run_pytest

""".format(pr=self.pr, run_pytest=run_pytest, run_env=run_env),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("infiniflow", "ragflow")
class Ragflow(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        # Every instance is constructed before the pipeline walks the dependency
        # graph, so by the time any base image is built the newest PR is known.
        _register_base_seed(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RagflowImageDefault(self.pr, self._config)

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

        def remove_ansi_escape_sequences(text: str) -> str:
            return re.compile(r"\x1B\[[0-?9;]*[mK]").sub("", text)

        test_log = remove_ansi_escape_sequences(test_log)

        # `pytest -v -rA` reports every outcome twice: once on the verbose
        # progress line ("test/unit_test/x.py::TestY::test_z PASSED [ 5%]") and
        # once in the short summary ("PASSED test/unit_test/x.py::TestY::test_z").
        # Both carry the node id, so either form yields the same identifier and
        # the sets deduplicate. A summary line such as
        # "SKIPPED [1] test/unit_test/x.py:12: reason" carries no node id, hence
        # the explicit "::" requirement -- matching it would otherwise record a
        # test literally named "[1]".
        # Node ids can contain spaces -- parametrised cases like
        # `test_token_count_ranges[hello world-2]` and
        # `test_invalid_floats[ 1.0]` are real ids in this suite. Matching with
        # `\S+` truncates them at the first space ("...ranges[hello"), which
        # both mangles the recorded id and can collapse two distinct cases onto
        # one key (`test_invalid_floats[ 1.0]` and `test_invalid_floats[1.0 ]`
        # both reduce to a prefix). Anchoring on the status keyword and the
        # trailing progress marker instead keeps the id intact.
        verbose_re = re.compile(
            r"^(?P<name>\S.*?::.*?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$"
        )
        # The short-summary form is "STATUS <node id>", optionally followed by
        # " - <reason>" for failures. The "::" requirement keeps lines such as
        # "SKIPPED [1] test/x.py:12: reason" (no node id) and
        # "ERROR test/x.py" (a whole-file collection error) out of the results.
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+"
            r"(?P<name>\S.*?::.*?)(?:\s+-\s.*)?$"
        )

        for raw_line in test_log.splitlines():
            line = raw_line.strip()

            match = verbose_re.match(line)
            if not match:
                match = summary_re.match(line)
            if not match:
                continue
            name, status = match.group("name"), match.group("status")

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # An id can surface under more than one status across the progress line
        # and the summary (a test that errors during teardown after passing, for
        # instance); resolve precedence deterministically so the buckets never
        # overlap.
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
