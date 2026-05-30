import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Single-era config covering PR #471 -> #5722 (release 0.16 -> 1.18).
# Although go.mod ranges from `go 1.16` to `go 1.26.0` and the module path
# was renamed twice (`aler9/rtsp-simple-server` -> `aler9/mediamtx` ->
# `bluenviron/mediamtx`), one config suffices because:
#   - GOTOOLCHAIN=auto on the golang:1.22 host downloads any newer toolchain
#     declared in go.mod at runtime (verified for go 1.24, 1.25, 1.26).
#   - GitHub auto-redirects the legacy aler9 clone URL to bluenviron/mediamtx.
#   - The test framework (Go built-in `testing`) and output format are stable
#     across the entire history, so parse_log() is unchanged.


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
        return "golang:1.22"

    def image_prefix(self) -> str:
        return "mswebench"

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
                "run_tests.sh",
                """#!/bin/bash
# Single-module Go repo. GOTOOLCHAIN=auto lets the host golang:1.22 download
# the toolchain version declared by go.mod (mediamtx spans go 1.16 -> 1.26+).
set -eo pipefail
export CI=true
export GOTOOLCHAIN=auto
cd /home/{pr.repo}
# `go generate ./...` produces embed assets (hls.min.js, mtxrpicam binaries,
# VERSION file) required for compilation. Without it, packages with
# `//go:embed` directives fail with "pattern X: no matching files found" and
# `go test` exits before printing any --- PASS/FAIL lines. This mirrors the
# repo's own scripts/test.mk which runs `go generate` before `go test`.
# Wrapped in `|| true` because some PRs may have transient network failures
# downloading external assets (hls.js CDN, GitHub releases for rpicam) — the
# test compile will surface any real missing-embed error.
go generate ./... || true
# `go test` always writes per-package result lines (`--- PASS:` / `--- FAIL:` /
# `--- SKIP:`) before exiting, so the script's exit status does not affect what
# parse_log sees. Capture status without `|| true`, then exit 0 so the harness
# always reaches parse_log even when some packages fail.
status=0
# `-p=1` runs packages serially; `-parallel=1` runs subtests within a package
# serially. Both are needed because mediamtx integration tests bind real ports
# (RTSP/RTMP/HLS/SRT/WebRTC) — concurrent runs cause port-conflict flakes that
# show up as Rule 2 (PASS->FAIL between test/fix stages) in the harness.
go test -v -count=1 -p=1 -parallel=1 -timeout=20m ./... || status=$?
echo "run_tests.sh: go test exited with status=$status"
exit 0
""".format(pr=self.pr),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

export GOTOOLCHAIN=auto
go mod download || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
bash /home/run_tests.sh
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
bash /home/run_tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # DockerfileEnhancer (image.py) wraps this output:
        #   1. Inserts its infrastructure block (ARGs for TARGETARCH/REPO_URL/
        #      BASE_COMMIT, proxy/SSL ENVs, LANG/TZ/DEBIAN_FRONTEND, OCI
        #      LABELs, cert symlinks) immediately after the FROM line.
        #   2. `_standardize_repo_fetch` REPLACES any `RUN git clone <url>
        #      /home/{repo}` line with the canonical fetch block:
        #         RUN git clone "${{REPO_URL}}" /home/{repo}
        #         WORKDIR /home/{repo}
        #         RUN git reset --hard
        #         RUN git checkout ${{BASE_COMMIT}}
        #         CMD ["/bin/bash"]
        # So this function MUST emit:
        #   - `FROM` line (anchor for #1)
        #   - `RUN git clone https://... /home/{repo}` (substitution anchor for #2)
        # and SHOULD NOT emit anything that would duplicate the enhancer's
        # output: no ENV DEBIAN_FRONTEND, no proxy/SSL, no COPY fix.patch or
        # COPY test.patch (enhancer doesn't add those for us, but having them
        # would duplicate something the harness manages elsewhere — they are
        # provided via Image.files() and the build context).
        script_files = [
            f for f in self.files() if f.name not in ("fix.patch", "test.patch")
        ]
        copy_commands = "\n".join(
            f"COPY {f.name} /home/" for f in script_files
        )

        # NOTE: NO blank line between `FROM` and the next instruction.
        # DockerfileEnhancer's `_infrastructure_block` already ends with "\n",
        # and `enhance()` appends our post-FROM lines verbatim — so any blank
        # line we put after `FROM` becomes a second consecutive blank in the
        # final output, which validate_dockerfiles.py flags as a warning.
        return f"""FROM golang:1.22
ENV GOTOOLCHAIN=auto

RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
        make gcc libc6-dev ffmpeg \\
 && rm -rf /var/lib/apt/lists/*

COPY fix.patch /home/
COPY test.patch /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

{copy_commands}

RUN bash /home/prepare.sh
"""


@Instance.register("bluenviron", "mediamtx")
class Mediamtx(Instance):
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

        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [
            re.compile(r"--- FAIL: (\S+)"),
            re.compile(r"FAIL:?\s?(.+?)\s"),
        ]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        def get_base_name(test_name: str) -> str:
            return test_name

        for line in test_log.splitlines():
            line = line.strip()

            for re_pass_test in re_pass_tests:
                pass_match = re_pass_test.match(line)
                if pass_match:
                    test_name = pass_match.group(1)
                    if test_name in failed_tests:
                        continue
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    passed_tests.add(get_base_name(test_name))

            for re_fail_test in re_fail_tests:
                fail_match = re_fail_test.match(line)
                if fail_match:
                    test_name = fail_match.group(1)
                    if test_name in passed_tests:
                        passed_tests.remove(test_name)
                    if test_name in skipped_tests:
                        skipped_tests.remove(test_name)
                    failed_tests.add(get_base_name(test_name))

            for re_skip_test in re_skip_tests:
                skip_match = re_skip_test.match(line)
                if skip_match:
                    test_name = skip_match.group(1)
                    if test_name in passed_tests:
                        continue
                    if test_name not in failed_tests:
                        continue
                    skipped_tests.add(get_base_name(test_name))

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
