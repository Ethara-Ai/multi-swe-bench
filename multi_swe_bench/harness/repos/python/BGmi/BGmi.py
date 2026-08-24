import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


TEST_CMD = (
    "pytest --no-header -rA --tb=short -p no:cacheprovider -v "
    "tests/test_utils.py tests/test_http_api.py "
    "tests/test_download.py tests/test_models.py"
)


class BGmiImageBase(Image):
    """Base image (`base-pr-<N>`): OS + Python + BGmi source pinned to BASE_COMMIT.

    This layer is per-PR (its tag carries the PR number) because it bakes in
    the code at that PR's exact base commit — the "before-fix" state the
    3-run contract compares against. It is intentionally patch-free: patches
    and run scripts live in ``BGmiImageDefault``.

    Because ``dependency()`` returns a **string** (`python:3.8-slim`), the
    pipeline's ``DockerfileEnhancer`` treats this file as a "leaf" and
    auto-injects the full reference infrastructure block on top of what
    ``dockerfile()`` returns:

      * ``# syntax=docker/dockerfile:1.6`` at line 1
      * ``ARG TARGETARCH``, ``ARG REPO_URL="https://github.com/BGmi/BGmi.git"``,
        ``ARG BASE_COMMIT``
      * The 7 proxy ARGs + ``CA_CERT_PATH``
      * The single ``ENV`` block wiring proxy passthrough + TLS bundle vars
        (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``)
      * The 4 OCI ``LABEL`` entries with ``authors="https://www.ethara.ai/"``
      * The CA-cert symlink farm (7 ``ln -sf`` lines), placed before any network RUN
      * The ``_HARDENING_BLOCK`` (git history scrub + 4 integrity asserts +
        submodule scrub) auto-inserted before the final ``CMD``.

    We therefore only spell out the **Python-specific** middle: apt toolchain,
    clone, checkout, ``pip install .``, test deps. Everything else the base
    Dockerfile needs is contributed by the enhancer.
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

    def dependency(self) -> str:
        # BGmi's pyproject.toml declares `python = "^3.8.0"` (>=3.8, <4.0).
        # python:3.8-slim satisfies the lower bound and provides manylinux
        # wheels for every runtime dep pinned in pyproject.toml
        # (pydantic 1.10.7, peewee 3.16.0, tornado 6.2, etc.). Debian-slim
        # (bullseye-based on 3.8) is multi-arch (amd64/arm64) and is *not*
        # on the DEPRECATED_DEBIAN_IMAGES list, so no apt-sources rewrite
        # is needed.
        return "python:3.8-slim"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        # The QC prompt (Step 0b + P1) requires the PR layer's FROM to point
        # at exactly ``mswebench/<org>_m_<repo>:base-pr-<N>``. Naming the base
        # tag ``base-pr-<N>`` — not just ``base`` — is what makes that
        # convention hold. The tag is per-PR because the base image carries
        # a per-PR clone (BASE_COMMIT differs between PRs).
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        # Mirrors image_tag() so the on-disk layout matches the QC prompt's
        # expectation: `./data1/work/BGmi/BGmi/images/base-pr-<N>/Dockerfile`.
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The base image is intentionally file-free. Patches and run scripts
        # are staged in ``BGmiImageDefault`` so the base remains a pristine
        # snapshot of the pre-fix repo state.
        return []

    def dockerfile(self) -> str:
        # apt package list rationale:
        #   git, ca-certificates      : mandatory for clone + TLS trust.
        #   build-essential           : several BGmi runtime deps
        #                               (peewee, tornado, filetype) ship
        #                               C-ext wheels but a source fallback
        #                               may be needed on arm64.
        #   libffi-dev, libssl-dev    : required by any cffi-based transitive
        #                               dep (cryptography → pyOpenSSL isn't
        #                               a BGmi dep, but qbittorrent-api
        #                               transitively pulls urllib3, which on
        #                               older Python 3.8 wheels may need
        #                               these headers when the wheel is
        #                               unavailable for the target arch).
        packages_str = (
            "git ca-certificates build-essential libffi-dev libssl-dev "
            "curl wget"
        )

        return f"""FROM {self.dependency()}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {packages_str} \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

# BGmi is Poetry-managed (build-backend = "poetry.core.masonry.api").
# `pip install .` triggers PEP 517 build isolation, pulls poetry-core into
# a scratch venv, builds the wheel, and installs BGmi with every runtime
# dep pinned in pyproject.toml. We *do not* install poetry itself here —
# the wheel build is fully self-contained.
RUN pip install --upgrade pip setuptools wheel
RUN pip install .

# Layer only the *test* deps that actually run the pytest suite.
# `pytest-github-actions-annotate-failures` from BGmi's [dev-dependencies]
# is intentionally omitted — it is a CI-only reporter and adds no value
# inside the harness sandbox.
RUN pip install pytest==7.2.2 "coverage[toml]==7.2.2" requests-cache==1.0.0 pytest-mock

# BGmi's tests/conftest.py::pytest_sessionstart unconditionally reads
# cfg.script_path (defaults to /root/.bgmi/scripts), so the directory
# must exist before pytest starts. Upstream CI runs `bgmi install` for
# this; we mirror that here but wrap with `|| true` because inside the
# hermetic sandbox the final step of `bgmi install` (fetching the SPA
# tarball from the npm registry) crashes with a schema mismatch — the
# registry response format changed since BGmi 3.x was written. That
# frontend is irrelevant to the test suite (only the on-disk directory
# skeleton is), and `bgmi install` creates the directories BEFORE it
# attempts the fetch, so `|| true` preserves the useful state.
RUN bgmi install || true
# `download_delegate=noop` swaps the real BitTorrent handshake for a
# null implementation so nothing in the suite attempts real network
# I/O. Wrapped defensively for the same reason as above.
RUN bgmi config set download_delegate --value noop || true

CMD ["/bin/bash"]
"""


class BGmiImageDefault(Image):
    """PR image (`pr-<N>`): the thin patch/scripts layer on top of ``base-pr-<N>``.

    Because ``dependency()`` returns an ``Image`` (not a string), the
    pipeline's ``DockerfileEnhancer`` skips this file (`image.py:315`),
    meaning what ``dockerfile()`` returns is *literally* what ends up on
    disk. That is by design and matches the PR-checklist (P1–P9) ideal:
    no runtime FROM, no clone, no apt, no scrub — just:

      * ``FROM mswebench/bgmi_m_bgmi:base-pr-<N>`` (P1)
      * ``COPY fix.patch /home/`` + ``COPY test.patch /home/`` (P2)
      * ``COPY run.sh`` / ``test-run.sh`` / ``fix-run.sh`` (P3)
      * ``COPY prepare.sh`` + ``COPY check_git_changes.sh`` + exactly one
        ``RUN bash /home/prepare.sh`` (P4).

    The base image already ships the repo pinned to BASE_COMMIT with the
    integrity asserts (D13/D14) enforced, so ``prepare.sh`` only has to
    re-verify the clean-tree invariant (P5) and drop a marker file.
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

    def dependency(self) -> Image:
        return BGmiImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                # Integrity guard invoked twice by prepare.sh — before and
                # after the re-checkout — so we catch a dirty working tree
                # at *either* end of the reset+checkout pair.
                """#!/bin/bash
set -e
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes detected"
  git status --porcelain
  exit 1
fi
echo "check_git_changes: working tree is clean"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                # Re-asserts the BASE_COMMIT pin at PR-build time (P5).
                # The base image already pinned this SHA via the enhancer's
                # hardening block (D13/D14); we repeat it here defensively
                # so `check_git_changes.sh` proves both:
                #   (a) the base image did not drift, and
                #   (b) no earlier PR-layer step left the tree dirty.
                # We also drop a `test_commands.sh` marker so the harness's
                # optional grep-based command extractor can locate the exact
                # pytest invocation without re-parsing this file.
                f"""#!/bin/bash
set -e
cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
echo '{TEST_CMD}' > test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                # Baseline / "run" of the 3-run contract: no patches applied.
                # Any test the target PR *adds* is not present at BASE_COMMIT,
                # so we do NOT expect it to appear in this run's output.
                # `CI=true` is exported per Phase 2C/3C guidance so pytest
                # plugins and any CI-gated test skips behave the same way
                # inside the sandbox as they do on GitHub Actions (where
                # the fix was originally proven).
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
{TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                # "test" run: test.patch ONLY (never fix.patch).
                # With the new tests staged but the code fix absent, the
                # f2p tests should FAIL — this is the signal the report
                # step diffs against fix-run.sh to prove the fix worked.
                # `--whitespace=nowarn` matches the harness convention and
                # tolerates the trailing-newline differences that GitHub's
                # patch export sometimes emits.
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed for test.patch" >&2
    exit 1
fi
{TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                # "fix" run: test.patch + fix.patch applied together, test
                # first (HOW_TO R3). With both patches in place every f2p
                # test must PASS — that transition (FAIL under test-run.sh
                # → PASS here) is what the report classifies as f2p.
                f"""#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{self.pr.repo}
if ! git -C /home/{self.pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed for test.patch + fix.patch" >&2
    exit 1
fi
{TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        # The PR-layer Dockerfile is intentionally tiny — its only job is
        # to stage the seven files above onto the pinned base image and
        # run prepare.sh once at build time. Everything heavier (proxy,
        # CA farm, clone, checkout, scrub) is owned by the base image.
        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in self.files()
        )

        return f"""FROM {self.dependency().image_full_name()}

{copy_commands}

RUN bash /home/prepare.sh
"""


@Instance.register("BGmi", "BGmi")
class BGmi(Instance):
    """Instance binding for BGmi/BGmi.

    Registration key ``BGmi/BGmi`` preserves the upstream repo casing exactly
    (HOW_TO rule R1). The dataset row's ``org``/``repo`` are both the
    capital-B, capital-G, capital-M, lowercase-i string ``BGmi``; the
    downstream ``image_name()`` implementation lowercases the org/repo when
    forming the Docker registry name, so the final image becomes
    ``mswebench/bgmi_m_bgmi:{tag}`` — which is what the QC prompt's
    ``mswebench/<org>_m_<repo>:base-pr-<N>`` convention requires.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return BGmiImageDefault(self.pr, self._config)

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
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()
        test_results: dict = {}

        # Strip ANSI escape sequences that pytest emits under `-v`.
        # Broader-than-SGR pattern (matches any CSI-final letter, per
        # Phase 4C guidance) so cursor / clear-line escapes are also
        # scrubbed — they'd otherwise leave stray bytes at the head of
        # a line and break the `^PASSED\s+` anchor.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # BGmi runs with `pytest -rA` which produces a short-summary
        # block of the form:
        #     PASSED tests/test_utils.py::test_something
        #     FAILED tests/test_save_path.py::test_missing_key - AssertionError
        #     ERROR  tests/test_boot.py::test_init - fixture 'x' not found
        #     SKIPPED [1] tests/test_legacy.py:42: reason
        # We *require* a `::` inside the captured test id so that
        # stray Python logging lines like
        #     "ERROR    bgmi.utils:__init__.py:88 something bad"
        # are never mis-parsed as pytest results.
        passed_pattern = re.compile(r"^PASSED\s+(\S+::\S+)")
        failed_pattern = re.compile(r"^FAILED\s+(\S+::\S+)")
        error_pattern = re.compile(r"^ERROR\s+(\S+::\S+)")
        # SKIPPED format: "SKIPPED [N] file.py:line: reason".
        # We anchor on the ":<space>" that follows the line number
        # so we don't consume the reason text into the test id.
        skipped_pattern = re.compile(r"^SKIPPED\s+\[\d+\]\s+(\S+?):\s")

        for line in clean_log.splitlines():
            line = line.strip()

            match = failed_pattern.match(line) or error_pattern.match(line)
            if match:
                test_results[match.group(1)] = "failed"
                continue

            match = skipped_pattern.match(line)
            if match:
                test_name = match.group(1)
                # A test that failed earlier in the log outranks a later
                # SKIP (this happens when pytest reruns collection).
                if test_results.get(test_name) != "failed":
                    test_results[test_name] = "skipped"
                continue

            match = passed_pattern.match(line)
            if match:
                test_name = match.group(1)
                # Precedence: failed > skipped > passed
                if test_results.get(test_name) not in ("failed", "skipped"):
                    test_results[test_name] = "passed"
                continue

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        # R2 (HOW_TO): guarantee the three sets are pairwise disjoint.
        # Failure always wins; a passing rerun of a failed test still
        # counts as failed for classification purposes.
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
