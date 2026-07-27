import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# =============================================================================
# Genymobile/scrcpy — C (meson), single era
# ubuntu:24.04, meson + ninja, debug build (tests require assertions)
#
# Java/Android server tests are excluded: they require the Android SDK which
# is not available in the harness Docker environment.  Only the C unit tests
# under app/tests/ are executed via `meson test`.
#
# Verified interactively in Docker against:
#   - latest  base commit c63d9e18 (PR #5870, scrcpy 3.1)
#   - earliest base commit bc750842 (PR #1224, scrcpy 1.12.1)
# Both build and test successfully with the same image and commands.
# =============================================================================


class ImageBase(Image):
    """scrcpy base: ubuntu:24.04, meson + ninja, ffmpeg/SDL2 dev libs, FULL clone.

    SHARED base (tag "base", ONE image reused by EVERY PR in this era) --
    clones the repo but does NOT check out any PR's base.sha and does NOT run
    the anti-reward-hack hardening. Both are per-PR concerns: hardening pins
    HEAD to one sha and runs `git gc --prune=now`, so doing that here would
    prune the shared clone down to a single PR's commit and break every other
    PR's `git checkout` with "reference is not a tree". Per-PR checkout +
    hardening live in ImageDefault instead (see its docstring) -- because
    Docker layers are copy-on-write, that layer's history-stripping never
    touches this shared base image, only the graded per-PR image built on
    top of it.

    The `# syntax` directive makes DockerfileEnhancer.enhance() emit this
    Dockerfile verbatim (same pattern as S2nTlsImageBase /
    SQLALCHEMY_13237_TO_11942's ImageBase) -- this is what makes cloning here
    safe: DockerfileEnhancer._inject_final_sanitize would otherwise
    auto-inject the checkout+prune hardening into *this* shared layer for
    *any* string-dependency Dockerfile containing "git clone", which is
    exactly the pinning this docstring says must not happen. Opting out also
    skips the enhancer's proxy ARGs / CA certificate symlinks / MITM mount,
    so the ARG TARGETARCH + LABEL block are replicated by hand so multi-arch
    buildx support and OCI labels aren't lost.

    No project-dependency install step here: scrcpy's dependencies are the
    apt packages below (ffmpeg/SDL2 dev libs, meson, ninja), not a lockfile
    inside the repo, so there is nothing analogous to `npm install` to run
    before checkout. The actual build (`meson setup`) only happens in the
    per-PR layer, since build flags differ by era/PR (compile_server vs
    prebuilt_server, buildtype, etc).
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
        return "ubuntu:24.04"

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

        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=UTC
ENV LANG=C.UTF-8

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}
WORKDIR /home/
ENV LC_ALL=C.UTF-8
RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    build-essential \\
    ca-certificates \\
    git \\
    meson \\
    ninja-build \\
    pkg-config \\
    libsdl2-dev \\
    libavcodec-dev \\
    libavdevice-dev \\
    libavformat-dev \\
    libavutil-dev \\
    libswresample-dev \\
    libusb-1.0-0-dev \\
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}
WORKDIR /home/{self.pr.repo}

{self.clear_env}

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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # -Dcompile_server=false: server requires Android SDK, not available
        # --buildtype=debug: tests are only compiled in debug mode (guarded
        #   by `if get_option('buildtype') == 'debug'` in app/meson.build)
        #
        # test.patch is a CUMULATIVE diff across this PR's whole bundle (up to
        # dozens of PRs), so it routinely deletes/renames a test source file
        # as part of a LATER bundled PR's reorg without updating meson.build's
        # `tests = [['name', ['file.c', ...]], ...]` list to match -- meson
        # configure then hard-fails ("File tests/x.c does not exist") for the
        # WHOLE project, not just that one test. Retry with that specific
        # test() entry stripped out of meson.build (matched by the missing
        # file's basename, both single- and multi-line entry styles) so every
        # OTHER test -- genuinely unaffected by whatever the bundle renamed --
        # still gets to build and report a real result instead of the entire
        # instance going to zero. A clean checkout never hits this (the loop
        # exits on the first successful configure), so this is a no-op for
        # run.sh's baseline build.
        build_cmd = r"""for i in 1 2 3 4 5; do
    out=$(meson setup build --buildtype=debug -Dcompile_server=false 2>&1) && { echo "$out"; break; }
    echo "$out"
    missing=$(echo "$out" | grep -oP 'ERROR: File \K\S+(?= does not exist)' | head -1)
    if [ -z "$missing" ]; then break; fi
    echo "meson.build references $missing, which this bundle's test patch doesn't provide -- stripping its test() entry and retrying"
    rm -rf build
    python3 - "$missing" << 'PYEOF'
import re, sys, os
missing = sys.argv[1]
basename = os.path.basename(missing)
path = "app/meson.build"
with open(path) as f:
    content = f.read()
pattern = re.compile(r"\[\s*'[^']*'\s*,\s*\[.*?\]\s*\]\s*,", re.DOTALL)
removed = [False]
def repl(m):
    if basename in m.group(0):
        removed[0] = True
        return ""
    return m.group(0)
new_content = pattern.sub(repl, content)
if removed[0]:
    with open(path, "w") as f:
        f.write(new_content)
PYEOF
done"""
        # `meson test` alone implicitly rebuilds via ninja with its default
        # fail-fast behavior (-k 1): one test file that fails to compile
        # aborts the WHOLE build, wiping out every other, unrelated test's
        # result (they all show as "did not run" even though they have
        # nothing to do with the broken one). `ninja -k 0` instead builds
        # everything it possibly can and only leaves the genuinely-broken
        # target(s) unbuilt; running `meson test --no-rebuild` against just
        # the binaries that exist gets honest pass/fail signal for every
        # test unaffected by whatever didn't compile, instead of a blanket
        # "no results" for the entire instance.
        test_cmd = """ninja -C build -k 0 || true
runnable=""
for t in $(meson test -C build --list 2>/dev/null); do
    if [ -x "build/app/$t" ]; then
        runnable="$runnable $t"
    fi
done
if [ -n "$runnable" ]; then
    meson test -C build --no-rebuild --print-errorlogs $runnable
else
    echo "No test binaries were successfully built (all test targets failed to compile/link)."
fi"""

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

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

""".format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{build_cmd}
{test_cmd}
""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{build_cmd}
{test_cmd}

""".format(repo=self.pr.repo, build_cmd=build_cmd, test_cmd=test_cmd),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        # The shared base already cloned the full repo (see ImageBase
        # docstring) and left WORKDIR at /home/{repo}, so prepare.sh below
        # just resets and checks out ${BASE_COMMIT} (baked in as a literal
        # SHA) against that existing clone -- no re-clone needed here. Because
        # this image's dependency() is another Image (not a string),
        # DockerfileEnhancer.enhance() leaves this Dockerfile untouched, so the
        # anti-reward-hacking git history strip has to be applied here
        # explicitly, on top of the base's layer (copy-on-write keeps the
        # shared base's full history intact for every other PR). ENV
        # BASE_COMMIT is set (literal, not a build ARG) purely so
        # Image._HARDENING_BLOCK's ${BASE_COMMIT} references resolve.
        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

WORKDIR /home/{self.pr.repo}
ENV BASE_COMMIT={self.pr.base.sha}

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


# =============================================================================
# Instance — default handler for Genymobile/scrcpy (all PRs, single era)
# =============================================================================


@Instance.register("Genymobile", "scrcpy")
class Scrcpy(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # Meson test output format (captured from real Docker run):
        #  1/11 test_adb_parser             OK              0.02s
        #  2/11 test_binary                 OK              0.02s
        #  3/11 test_audiobuf               OK              0.02s
        # 11/11 test_cli                    OK              0.02s
        re_result = re.compile(
            r"^\s*\d+/\d+\s+(.+?)\s+(OK|FAIL|SKIP|EXPECTEDFAIL|TIMEOUT|ERROR)\s+[\d.]+s"
        )

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            match = re_result.match(line)
            if match:
                test_name = match.group(1)
                status = match.group(2)
                if status == "OK":
                    passed_tests.add(test_name)
                elif status in ("FAIL", "TIMEOUT", "ERROR"):
                    failed_tests.add(test_name)
                elif status in ("SKIP", "EXPECTEDFAIL"):
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
