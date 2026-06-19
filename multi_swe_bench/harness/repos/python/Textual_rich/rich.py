import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Textualize/rich — a Python library for rich text & formatting in the terminal.
#
# Conformed to the hardened image.py contract:
#   * dependency() returns a *string* base image, so the shared
#     Image.dockerfile() owns the build — it clones "${REPO_URL}", checks out
#     "${BASE_COMMIT}", and appends the _HARDENING_BLOCK that detaches HEAD and
#     strips every other ref/remote/commit. That closes the reward-hacking hole
#     where the fix could be read straight out of git history (git log / show /
#     diff origin).
#   * SELF-CONTAINED, proxy/cert-free: dockerfile() below emits the *complete*
#     Dockerfile (including the "# syntax=docker/dockerfile:1.6" directive and a
#     hand-rolled infra block with NO proxy / CA-cert / MITM lines). Because
#     DockerfileEnhancer.enhance() returns any Dockerfile that already carries
#     that syntax directive unchanged, the enhancer's proxy/cert injection never
#     runs for rich — regardless of which image.py builds it (even a tree whose
#     image.py still injects the MITM proxy). The proxy removal is a property of
#     this registry, not of the shared image.py.
#   * extra_setup() runs after "git checkout ${BASE_COMMIT}" and before the
#     hardening pass. We install rich (editable) at the base commit and COPY the
#     eval scripts + patches into /home/ (outside /home/rich, so the hardening
#     pass — which only touches the git tree — leaves them alone). Baking them
#     in is required because docker_util.run mounts *only* fix.patch at runtime.
#
# Single pure-Python package (rich/) with a root poetry pyproject and a root
# tests/ tree. pytest is the runner throughout -> one parse_log. No services /
# no API keys. Installed with system pip (no venv / no uv): the host build is
# native arm64 and pip keeps the git tree clean so the hardening checkout holds.


# git apply excludes: rich's test/fix patches carry binary blobs (SVG/PNG/etc.
# rendered-output fixtures) whose diff hunks abort an otherwise-good apply.
_EXCLUDES = (
    "--exclude=*.png --exclude=*.jpg --exclude=*.jpeg --exclude=*.gif "
    "--exclude=*.ico --exclude=*.svg --exclude=*.pdf --exclude=*.zip"
)


def _target_test_files(test_patch: str) -> list[str]:
    """Test files the test_patch touches — the only ones worth running.

    Scoping to these (instead of the whole tests/ tree) is the standard SWE-bench
    F2P/P2P convention; it also sidesteps unrelated tests in recent rich versions
    that hang forever on a tty/stdin read. We read the destination ("+++ b/...")
    side so newly-added test files are captured too (get_modified_files drops
    them, since their source is /dev/null). Deleted files map to +++ /dev/null and
    are naturally excluded.
    """
    files: list[str] = []
    for dst in re.findall(r"^\+\+\+ b/(.+?)\s*$", test_patch, re.M):
        if dst.endswith(".py") and (dst.startswith("tests/") or "/test" in dst or dst.startswith("test")):
            if dst not in files:
                files.append(dst)
    return files


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

    def dependency(self) -> str:
        # String dependency -> Image.dockerfile() clones, checks out
        # ${BASE_COMMIT}, and appends the hardening block. See module docstring.
        return "python:3.12-bookworm"

    def dockerfile(self) -> str:
        # Self-contained Dockerfile generation: emit the COMPLETE Dockerfile with
        # our own infra block — NO proxy args, NO CA-cert symlinks, NO MITM mount.
        #
        # The shared Image.dockerfile() body (FROM + apt + clone + checkout +
        # extra_setup + hardening + CMD) never carries proxy/cert — that is added
        # only by DockerfileEnhancer. We prepend the "# syntax=..." directive,
        # which makes DockerfileEnhancer.enhance() return this string unchanged
        # (see its early-return on SYNTAX_DIRECTIVE). Result: rich images are
        # proxy/cert-free no matter which image.py builds them.
        body = super().dockerfile()
        org, repo = self.pr.org, self.pr.repo
        repo_url = f"https://github.com/{org}/{repo}.git"

        infra = (
            "ARG TARGETARCH\n"
            f'ARG REPO_URL="{repo_url}"\n'
            "ARG BASE_COMMIT\n"
            "\n"
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    TZ=UTC\n"
            "\n"
            f'LABEL org.opencontainers.image.title="{org}/{repo}" \\\n'
            f'      org.opencontainers.image.description="{org}/{repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        lines = body.split("\n")
        from_idx = next(
            i for i, ln in enumerate(lines) if ln.strip().upper().startswith("FROM ")
        )
        out = ["# syntax=docker/dockerfile:1.6", ""]
        out += lines[: from_idx + 1]
        out += ["", infra]
        out += lines[from_idx + 1 :]
        return "\n".join(out)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        # rich is pure Python; the default package set (git, build-essential,
        # curl, ca-certificates, python3, make, ...) already covers the build.
        return []

    def extra_setup(self) -> str:
        # Bake the eval scripts + both patches in, then warm the install at the
        # base commit so deps live in an image layer. install.sh is reused at run
        # time in case a patch adds a dep.
        #
        # fix.patch MUST be baked: build_dataset.run_instance calls
        # docker_util.run() with NO volumes, so unlike run_evaluation it does not
        # mount fix.patch at runtime — the fix stage reads the baked
        # /home/fix.patch. (Omitting it makes the fix stage identical to the test
        # stage -> F2P=0 for every instance.) This is the framework convention
        # (langchain/astropy bake it too). The new-image.py anti-cheat win is the
        # git-history scrub (_HARDENING_BLOCK), which is unaffected.
        return (
            "ENV PIP_ROOT_USER_ACTION=ignore\n"
            "ENV PIP_DISABLE_PIP_VERSION_CHECK=1\n"
            "COPY fix.patch /home/fix.patch\n"
            "COPY test.patch /home/test.patch\n"
            "COPY install.sh /home/install.sh\n"
            "COPY run.sh /home/run.sh\n"
            "COPY test-run.sh /home/test-run.sh\n"
            "COPY fix-run.sh /home/fix-run.sh\n"
            "RUN bash /home/install.sh || true\n"
        )

    def files(self) -> list[File]:
        repo = self.pr.repo

        install = """#!/bin/bash
# Editable install of rich at the current checkout. The [jupyter] extra covers
# the jupyter tests; fall back to a bare install if the extra is unavailable.
# Always (re)install pytest + common plugins so the runner is present.
set -uo pipefail
cd /home/__REPO__
pip install --no-cache-dir -e ".[jupyter]" >/dev/null 2>&1 \\
  || pip install --no-cache-dir -e . >/dev/null 2>&1 || true
# pytest-timeout is mandatory: some rich versions ship tests that block forever
# on a tty/stdin read or a live-display loop, which would otherwise hang the
# whole suite. A per-test timeout kills only the offending test.
pip install --no-cache-dir pytest pytest-asyncio pytest-mock pytest-timeout >/dev/null 2>&1 || true
""".replace("__REPO__", repo)

        target_tests = " ".join(_target_test_files(self.pr.test_patch))

        # Re-run install (a patch may add a dependency), select the patch's own
        # test files that actually exist on disk (the unpatched baseline run
        # won't have newly-added ones yet), then pytest only those. -v gives one
        # `path::test STATUS` line per test for parse_log; -o addopts="" discards
        # the coverage/plugin flags rich pins in pyproject.
        #
        # --timeout=30 (signal method) caps each test at 30s and marks an
        # over-runner FAILED, then continues. rich's tests run in well under a
        # second, so 30s is generous headroom; the cap matters because some PRs
        # FIX an infinite loop (e.g. chop_cells on zero-width input) -> at the
        # base commit those new tests genuinely hang, and must register as a
        # bounded FAILED (the F2P signal) instead of blocking the run. The outer
        # `timeout` is a coarse backstop for a hang during collection, which the
        # per-test timeout can't catch.
        run_pytest = """bash /home/install.sh || true
TARGET="__TARGET__"
SEL=""
for f in $TARGET; do [ -e "$f" ] && SEL="$SEL $f"; done
if [ -z "${SEL// }" ]; then
    echo ">>>>> No target test files present; nothing to run."
else
    timeout -k 30 1200 python -m pytest $SEL -v -rA --continue-on-collection-errors \\
        -o addopts="" -p no:cacheprovider --timeout=30 --timeout-method=signal || true
fi""".replace("__TARGET__", target_tests)

        run_sh = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__RUN_PYTEST__", run_pytest)

        test_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch \\
  || echo "git apply test.patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        fix_run = """#!/bin/bash
set -uo pipefail
export CI=true
cd /home/__REPO__
git apply --3way --whitespace=nowarn __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || git apply --whitespace=nowarn --reject __EXCLUDES__ /home/test.patch /home/fix.patch \\
  || echo "git apply test+fix patch failed (continuing)"
__RUN_PYTEST__
""".replace("__REPO__", repo).replace("__EXCLUDES__", _EXCLUDES).replace(
            "__RUN_PYTEST__", run_pytest
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "install.sh", install),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]


@Instance.register("Textualize", "rich")
class Rich(Instance):
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
        # Strip real ANSI escape sequences (note: a nodeid may contain the
        # *literal* text "\x1b" — pytest escapes control chars in param ids —
        # which this leaves intact).
        ansi = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")
        clean = ansi.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Parse ONLY pytest's `-v` progress lines, anchored on the unambiguous
        # "[ NN%]" suffix:
        #   tests/test_text.py::test_wrap PASSED                       [ 12%]
        #   tests/test_cells.py::test_chop[\x1b-expected1] FAILED      [ 40%]
        #   tests/test_x.py::test_skip SKIPPED (reason)                [ 50%]
        # The nodeid is captured non-greedily so params that themselves contain
        # spaces or " - " (e.g. test_split_text[... - x]) stay whole. The `-rA`
        # summary lines ("FAILED <nodeid> - <reason>") are deliberately ignored:
        # the trailing reason and in-param " - " make them ambiguous, and every
        # test that ran already has a `-v` line. This is what previously double-
        # counted parametrized tests under "name" and "name]".
        line_re = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
            r"(?:\s+\(.*?\))?\s+\[\s*\d+%\]\s*$"
        )

        for line in clean.splitlines():
            m = line_re.match(line.rstrip())
            if not m:
                continue
            name = m.group(1).strip()
            status = m.group(2)
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status in ("SKIPPED", "XFAIL"):
                skipped_tests.add(name)

        # Disjoint sets: failed > skipped > passed.
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
