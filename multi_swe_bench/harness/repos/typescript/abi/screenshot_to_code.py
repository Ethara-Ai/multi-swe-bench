import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Level 1: toolchain-only base image, shared by every PR of this repo.

    ``dependency()`` returns a *string* (the Node toolchain image), which would
    normally make ``DockerfileEnhancer`` engage and prepend its
    ``# syntax``/ARG/ENV/LABEL/proxy/cert infra block. This Dockerfile carries
    the ``# syntax`` directive itself, so ``DockerfileEnhancer.enhance``
    short-circuits and returns it verbatim: the ARG/ENV/LABEL header below is
    hand-written and deliberately carries NO proxy build args
    (``http_proxy``/``HTTPS_PROXY``/``no_proxy``/``CA_CERT_PATH``), NO
    TLS-redirect env vars (``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``/
    ``CURL_CA_BUNDLE``), NO ``/etc/pki`` CA-bundle symlink block and NO
    ``mitm_ca`` secret mount. Builds use the distro's own trust store from the
    ``ca-certificates`` package alone.

    IMPORTANT: this image must NOT clone the repository. A shared,
    string-dependency image that performs a ``git clone`` is rewritten by
    ``DockerfileEnhancer._standardize_repo_fetch`` into a clone +
    ``git checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` sequence. The
    base is built once (tag ``base``) with whichever PR's ``base.sha`` happens
    to build first, so the history strip would pin the shared base to that one
    commit and delete every other PR's ``base.sha`` — breaking ``git checkout``
    for all remaining PRs. The clone therefore lives in ``ImageDefault``
    (whose ``dependency()`` is an ``Image``, left verbatim by the enhancer) and
    is done per-PR. This image only provides the Node/Python toolchain.
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
        return "node:22-bookworm"

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

        # No `git clone` here on purpose (see class docstring), and the `# syntax`
        # directive below makes DockerfileEnhancer return this file verbatim, so
        # the hand-written proxy-free/cert-free header is what actually ships.
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    TZ=UTC

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git curl jq ca-certificates \\
        python3 python3-pip python3-venv python3-dev pipx \\
        build-essential pkg-config \\
    && rm -rf /var/lib/apt/lists/*
RUN pipx ensurepath && pipx install poetry
ENV PATH="/root/.local/bin:${{PATH}}"
RUN pip3 install --break-system-packages --quiet pytest pytest-asyncio || true
RUN npm install -g yarn pnpm@9 || true
RUN git config --global --add safe.directory '*'

{self.clear_env}

CMD ["/bin/bash"]
"""


class ImageDefault(Image):
    """Level 2: per-PR image, built on the shared toolchain base.

    ``dependency()`` returns ``ImageBase`` (an ``Image``, not a string), so
    ``DockerfileEnhancer.enhance`` returns this Dockerfile verbatim — the
    pipeline injects neither a pin nor a history strip here. Both therefore
    live in this Dockerfile, where pinning is correct because it is per-PR:

      1. clone full history and check out ``${BASE_COMMIT}`` (defaulted to this
         PR's ``base.sha``, so the build needs no external build-arg),
      2. run ``prepare.sh`` to warm the poetry/yarn caches while the full
         clone is still present,
      3. apply the verbatim ``Image._HARDENING_BLOCK`` — detach at
         ``${BASE_COMMIT}``, drop origin, delete every ref, expire reflogs,
         gc/repack, drop alternates, then the four post-condition asserts and
         the submodule pass.

    After (3) the container holds base.sha and its ancestors only: no origin to
    fetch from, no future commits, no branches/tags/reflog to recover the gold
    fix from. That is the anti-reward-hacking contract of the harness.
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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
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
                # The checkout of {pr.base.sha} is done inline in the Dockerfile
                # (RUN git checkout ${BASE_COMMIT}) and the history strip is the
                # inline Image._HARDENING_BLOCK that runs AFTER this script.
                # prepare.sh only warms the dependency caches.
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

# Backend: install Python deps via poetry (best-effort)
if [ -f backend/pyproject.toml ]; then
  cd backend
  poetry env use "$(which python3)" || true
  poetry install --no-interaction --no-root || true
  # the runner invokes pytest inside the poetry env; make sure the test
  # dependencies exist there even on revisions that omit them from pyproject
  poetry run python -m pip install --quiet pytest pytest-asyncio || true
  cd ..
fi

# Frontend: install Node deps (best-effort; favor yarn since lockfiles are yarn)
if [ -f frontend/package.json ]; then
  cd frontend
  if [ -f yarn.lock ]; then
    yarn install --frozen-lockfile || yarn install || true
  elif [ -f package-lock.json ]; then
    npm ci || npm install || true
  else
    yarn install || npm install || true
  fi
  cd ..
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run-tests.sh",
                (
                    """#!/bin/bash
set +e

REPO_DIR="/home/__REPO__"
cd "$REPO_DIR"

echo "===== BACKEND PYTEST ====="
if [ -d backend ] && find backend -name 'test_*.py' -print -quit 2>/dev/null | grep -q .; then
  cd backend
  # backend tests import top-level modules (`from routes.generate_code import ...`),
  # so the backend dir must be importable. `python -m pytest` prepends CWD and
  # PYTHONPATH covers the poetry-env case.
  export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
  # Exactly ONE invocation: a `cmd || fallback_cmd` chain would re-run the whole
  # suite whenever a test legitimately fails, duplicating results in the parsed
  # log and making the run non-deterministic.
  #
  # --continue-on-collection-errors is load-bearing for grading. The gold test
  # patch adds tests importing modules the fix patch introduces, so at the
  # test-patch stage those modules do not exist yet and the imports raise. By
  # default pytest answers a collection error with
  #   !!!! Interrupted: N errors during collection !!!!
  # and executes NOTHING -- the whole suite is lost, every test reads as absent
  # rather than failing, and a test that would have been an honest F2P is
  # downgraded to an unverifiable N2P. Continuing runs every importable test, so
  # the pre-fix state of each one is actually observed.
  PYTEST_ARGS="-v --continue-on-collection-errors"
  if [ -f pyproject.toml ] && command -v poetry >/dev/null 2>&1 && poetry env info -p >/dev/null 2>&1; then
    poetry run python -m pytest $PYTEST_ARGS 2>&1
  else
    python3 -m pytest $PYTEST_ARGS 2>&1
  fi
  cd "$REPO_DIR"
fi

echo "===== FRONTEND TESTS ====="
if [ -d frontend ] && [ -f frontend/package.json ]; then
  cd frontend
  TEST_SCRIPT=$(node -e "try{var p=require('./package.json');process.stdout.write(String((p.scripts&&p.scripts.test)||''))}catch(e){}" 2>/dev/null)
  if [ -n "$TEST_SCRIPT" ]; then
    # Per-test granularity differs by runner; pick the right verbose flag once
    # rather than retrying with a second full run.
    case "$TEST_SCRIPT" in
      *vitest*) CI=true yarn test --run --reporter=verbose 2>&1 ;;
      *jest*)   CI=true yarn test --verbose 2>&1 ;;
      *)        CI=true yarn test 2>&1 ;;
    esac
  fi
  cd "$REPO_DIR"
fi

exit 0

"""
                ).replace("__REPO__", self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared toolchain base does NOT clone, so this per-PR image clones
        # full history first and checks out ${BASE_COMMIT} inline. Because this
        # image's dependency() is an Image, the DockerfileEnhancer returns the
        # Dockerfile verbatim -- the clone + hardening below are kept as written.
        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        # Anti-reward-hacking hardening -- the canonical Image._HARDENING_BLOCK,
        # concatenated raw (not through an f-string) so its ${BASE_COMMIT} and
        # %(refname) tokens stay literal in the generated Dockerfile.
        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("abi", "screenshot-to-code")
class ScreenshotToCode(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_re = re.compile(r"\x1b\[[0-9;]*m")

        # pytest verbose: "tests/test_foo.py::TestClass::test_name PASSED [ 12%]"
        re_pytest = re.compile(
            r"^(\S+::\S+|\S+\.py(?:::\S+)?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )

        # pytest collection failure, status-FIRST and therefore invisible to
        # re_pytest above:
        #   "ERROR tests/test_token_usage.py - ModuleNotFoundError: No module ..."
        #   "ERROR tests/test_token_usage.py::TestX"
        # A module that cannot even be imported is a failure of every test it
        # holds; recording the module keeps that fact in the parsed result
        # instead of silently dropping it (which reads as "test absent").
        re_pytest_collect_error = re.compile(
            r"^ERROR\s+(\S+\.py(?:::\S+)?)\s*(?:-.*)?$"
        )

        # jest default reporter: "PASS src/foo/bar.test.ts" / "FAIL src/foo/bar.test.ts"
        re_jest_file = re.compile(
            r"^(PASS|FAIL)\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b"
        )

        # jest verbose / vitest per-test: "✓ test name (12 ms)" or "✕ test name"
        re_jest_pass_test = re.compile(r"^\s*✓\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_jest_fail_test = re.compile(r"^\s*✕\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_jest_skip_test = re.compile(r"^\s*○\s+skipped\s+(.+?)\s*$")

        # vitest: "✓ file.test.ts (N tests)" / "× file.test.ts"
        re_vitest_pass = re.compile(
            r"^\s*✓\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b"
        )
        re_vitest_fail = re.compile(
            r"^\s*[×✗]\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx))\b"
        )

        for raw_line in test_log.splitlines():
            line = ansi_re.sub("", raw_line)
            stripped = line.strip()
            if not stripped:
                continue

            m = re_pytest.match(stripped)
            if m:
                name, status = m.group(1), m.group(2)
                if status == "PASSED" or status == "XPASS":
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                elif status in ("SKIPPED", "XFAIL"):
                    skipped_tests.add(name)
                continue

            m = re_pytest_collect_error.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_jest_file.match(stripped)
            if m:
                status, name = m.group(1), m.group(2)
                if status == "PASS":
                    passed_tests.add(name)
                else:
                    failed_tests.add(name)
                continue

            m = re_vitest_pass.match(stripped)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_vitest_fail.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

            m = re_jest_skip_test.match(stripped)
            if m:
                skipped_tests.add(m.group(1))
                continue

            m = re_jest_pass_test.match(stripped)
            if m:
                passed_tests.add(m.group(1))
                continue

            m = re_jest_fail_test.match(stripped)
            if m:
                failed_tests.add(m.group(1))
                continue

        # A test that both passed and failed somewhere in the log is a parse
        # ambiguity (e.g. a retried or re-reported case); count it as failed so a
        # flake can never be credited as an F2P transition.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval routing for the bundled (LHT) records
#
# `Instance.create` (harness/instance.py:42-46) dispatches on
# f"{org}/{pr.number_interval}" whenever `number_interval` is non-empty, and only
# falls back to f"{org}/{repo}" when it is "". So a bundled record does NOT reach
# the "abi/screenshot-to-code" key registered above -- it resolves to its own
# interval string, and an interval that is not registered raises
#     ValueError: Instance 'abi/210-257' is not registered.
# Registering the class under each interval keeps `number_interval` populated on
# the PullRequest, which is what `Dataset.build` (harness/dataset.py:63) copies
# into the resolved dataset jsonl.
#
# The interval is the dash-joined *enumeration* of prs_in_bundle
# (collect/build_lht_dataset.py:499-501) -- never a lo-hi range. Bundle
# [146, 147, 150, 155, 157] is "146-147-150-155-157", NOT "146-157": the range
# form would silently claim 148, 149, 151-154 and 156, which are not in the
# bundle. Derived here rather than pasted as literals so the two can never drift.
# ---------------------------------------------------------------------------

# fmt: off
_PRS_IN_BUNDLE: list[list[int]] = [
    [210, 257],
    [227, 358],
    [317, 329, 337, 341, 348, 353, 355],
    [511, 512, 515],
    [536, 537, 540, 541, 542, 543, 547, 548, 549],
    [539, 544, 550, 551, 553, 556, 559],
    [560, 569, 577, 583],
    [562, 567, 568, 570, 571, 574, 576],
    [4, 7, 19, 21, 25, 36, 37, 41, 43, 51, 58, 59, 62, 63, 64, 65, 70, 79, 94,
     99, 115, 119, 122, 139, 158, 160, 169, 170, 177],
]
# fmt: on


def number_interval(prs_in_bundle: list[int]) -> str:
    """Canonical `number_interval`: sorted, de-duplicated, dash-joined PR numbers.

    Mirrors ``"-".join(str(n) for n in sorted(pr_numbers))`` in
    ``build_lht_dataset.py``. Enumerates every PR in the bundle; it is not a range.
    """
    return "-".join(str(n) for n in sorted(set(prs_in_bundle)))


for _bundle in _PRS_IN_BUNDLE:
    Instance.register("abi", number_interval(_bundle))(ScreenshotToCode)


# ---------------------------------------------------------------------------
# number_interval backfill into the resolved dataset jsonl
#
# The shipped LHT file carries `prs_in_bundle` but no `number_interval`, so
# `Dataset.build` (harness/dataset.py:63) would copy an empty string into every
# resolved record. Two guarded patches close that, mirroring the convention in
# repos/rust/bytecodealliance/wasmtime.py:
#
#   1. `PullRequest.from_json` (the loader used by build_dataset.py:424 and
#      gen_report.py:267) stashes `prs_in_bundle` on the PR as `_prs_in_bundle`.
#      It deliberately does NOT set `number_interval`, so `Instance.create`
#      routing is unaffected for inputs that omit the field.
#   2. `Dataset.build` fills `number_interval` from that stash when the field is
#      still empty, so the resolved jsonl always carries the dash-joined
#      enumeration.
#
# An input that already carries `number_interval` (as build_lht_dataset.py:588
# emits) is left untouched by both patches and routes via the aliases above.
#
# Sentinels are checked against each class's OWN __dict__: Dataset subclasses
# PullRequest, so a getattr check would see the inherited from_json sentinel and
# wrongly skip Dataset's own patch.
# ---------------------------------------------------------------------------
import json as _json

from multi_swe_bench.harness.dataset import Dataset as _Dataset

if "_abi_stc_from_json_patch" not in PullRequest.__dict__:
    _orig_pr_from_json = PullRequest.from_json.__func__

    @classmethod
    def _pr_from_json_with_bundle(cls, json_str):
        pr = _orig_pr_from_json(cls, json_str)
        try:
            raw = _json.loads(json_str)
        except ValueError:
            return pr
        bundle = raw.get("prs_in_bundle") if isinstance(raw, dict) else None
        if bundle:
            # stash only -- number_interval stays "" so routing is unaffected
            pr._prs_in_bundle = list(bundle)
        return pr

    PullRequest.from_json = _pr_from_json_with_bundle
    PullRequest._abi_stc_from_json_patch = True


if "_abi_stc_build_patch" not in _Dataset.__dict__:
    _orig_dataset_build = _Dataset.build.__func__

    @classmethod
    def _dataset_build_with_interval(cls, pr, report):
        ds = _orig_dataset_build(cls, pr, report)
        # Scoped to this repo: other registries join their bundles in their own
        # order, so this must not silently re-derive theirs.
        if (
            not getattr(ds, "number_interval", "")
            and getattr(pr, "org", "") == "abi"
            and getattr(pr, "repo", "") == "screenshot-to-code"
        ):
            bundle = getattr(pr, "_prs_in_bundle", None)
            if bundle:
                ds.number_interval = number_interval(bundle)
        return ds

    _Dataset.build = _dataset_build_with_interval
    _Dataset._abi_stc_build_patch = True
