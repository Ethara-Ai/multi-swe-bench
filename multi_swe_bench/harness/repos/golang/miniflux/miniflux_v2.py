import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# miniflux/v2 spans three module states:
#   - pre-Aug 2018 (PR <~ 223): no go.mod, uses `dep` (Gopkg.toml) + vendor/.
#     Internal imports use `github.com/miniflux/miniflux/...` (the old org/repo
#     name), so the source must live at $GOPATH/src/github.com/miniflux/miniflux.
#   - Aug 2018 -> Aug 2023: go modules with `module miniflux.app`.
#   - post Aug 2023: go modules with `module miniflux.app/v2` (current repo URL
#     `github.com/miniflux/v2`).
# Modules-era and v2-era share the same build flow (`go test ./...` from the
# repo root), so they are merged into a single `v2_224_to_99999` era. The
# pre-modules era uses GOPATH style via `prepare_style="gopath"`.


class MinifluxVersionBase(Image):
    """Version-bucketed base image: FROM golang:{version} + clone repo."""

    def __init__(self, pr: PullRequest, config: Config, go_version: str, interval_name: str):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return f"golang:{self._go_version}"

    def image_tag(self) -> str:
        return f"base-{self._interval_name}"

    def workdir(self) -> str:
        return f"base-{self._interval_name}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}
ENV GOTOOLCHAIN=auto

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates make gcc libc6-dev \\
 && rm -rf /var/lib/apt/lists/* || true

WORKDIR /home/

{code}
"""


_COMMON_ENV = """export CI=true
export GOTOOLCHAIN=auto
export CGO_ENABLED=1"""


# Era 1: pre-modules. Source must live at $GOPATH/src/github.com/miniflux/miniflux
# because the code's internal imports still reference the old miniflux/miniflux
# repo path. The clone lands in /home/v2 (matches pr.repo), so we symlink it
# into the canonical GOPATH location.
_GOPATH_SETUP = """export GOPATH=/go
REPO_PATH="$GOPATH/src/github.com/miniflux/miniflux"
mkdir -p "$(dirname "$REPO_PATH")"
if [ ! -e "$REPO_PATH" ]; then
    ln -s /home/{pr.repo} "$REPO_PATH"
fi
cd "$REPO_PATH" """


class MinifluxImageDefault(Image):
    """Per-PR image: FROM version-base -> checkout + prepare.

    prepare_style:
      - "modules" (default): cd into /home/{repo}; -mod=mod ignores legacy
        vendor/modules.txt that is inconsistent under newer Go.
      - "gopath":  pre-modules; symlink clone into $GOPATH/src/github.com/miniflux/miniflux,
        run with GO111MODULE=off and the existing vendor/ dir.
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        go_version: str,
        interval_name: str,
        prepare_style: str = "modules",
    ):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name
        self._prepare_style = prepare_style

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return MinifluxVersionBase(self.pr, self.config, self._go_version, self._interval_name)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _modules_files(self) -> list[File]:
        env = _COMMON_ENV
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

{env}

cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}

# Legacy vendor/modules.txt (from earlier Go versions) is inconsistent under
# Go 1.21+. -mod=mod ignores it and fetches modules normally.
export GOFLAGS="-mod=mod"

go mod download 2>&1 || echo "go mod download failed (non-fatal)"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GOFLAGS="-mod=mod"

cd /home/{self.pr.repo}
status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "run.sh: go test exited with status=$status"
exit 0
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GOFLAGS="-mod=mod"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "test-run.sh: go test exited with status=$status"
exit 0
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GOFLAGS="-mod=mod"

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "fix-run.sh: go test exited with status=$status"
exit 0
""",
            ),
        ]

    def _gopath_files(self) -> list[File]:
        env = _COMMON_ENV
        gopath_setup = _GOPATH_SETUP.format(pr=self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

{env}

cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}

{gopath_setup}

# Pre-modules: vendor/ holds all deps; turn modules off.
export GO111MODULE=off
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GO111MODULE=off

{gopath_setup}

status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "run.sh: go test exited with status=$status"
exit 0
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GO111MODULE=off

# Apply patches at the real clone path (where git history lives), then run
# tests at the GOPATH-staged symlink (same files).
cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

{gopath_setup}

status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "test-run.sh: go test exited with status=$status"
exit 0
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}
export GO111MODULE=off

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}

{gopath_setup}

status=0
go test -v -count=1 -timeout 20m ./... || status=$?
echo "fix-run.sh: go test exited with status=$status"
exit 0
""",
            ),
        ]

    def files(self) -> list[File]:
        if self._prepare_style == "gopath":
            return self._gopath_files()
        return self._modules_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_PASS = re.compile(r"--- PASS: (\S+)")
_RE_FAIL = re.compile(r"--- FAIL: (\S+)")
_RE_SKIP = re.compile(r"--- SKIP: (\S+)")
# Package summary lines from `go test`:
#   "ok      miniflux.app/v2/internal/pkg  0.123s"
#   "FAIL    miniflux.app/v2/internal/pkg  0.123s"
#   "?       miniflux.app/v2/internal/pkg  [no test files]"
_RE_PKG_OK = re.compile(r"^ok\s+(\S+)")
_RE_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)")
_RE_PKG_NOTEST = re.compile(r"^\?\s+(\S+)")


def miniflux_parse_log(test_log: str) -> TestResult:
    """Buffer per-package outcomes; flush with package prefix on summary line.

    Go subtest names are not globally unique across packages, so we qualify
    each test name with its containing package path.
    """
    test_log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buf: list[tuple[str, str]] = []

    def flush(pkg: str) -> None:
        for status, name in buf:
            qualified = f"{pkg}::{name}"
            if status == "PASS":
                if qualified not in failed_tests:
                    skipped_tests.discard(qualified)
                    passed_tests.add(qualified)
            elif status == "FAIL":
                passed_tests.discard(qualified)
                skipped_tests.discard(qualified)
                failed_tests.add(qualified)
            elif status == "SKIP":
                if qualified not in passed_tests and qualified not in failed_tests:
                    skipped_tests.add(qualified)
        buf.clear()

    for line in test_log.splitlines():
        line = line.strip()

        m = _RE_PASS.match(line)
        if m:
            buf.append(("PASS", m.group(1)))
            continue
        m = _RE_FAIL.match(line)
        if m:
            buf.append(("FAIL", m.group(1)))
            continue
        m = _RE_SKIP.match(line)
        if m:
            buf.append(("SKIP", m.group(1)))
            continue

        m = _RE_PKG_OK.match(line) or _RE_PKG_FAIL.match(line) or _RE_PKG_NOTEST.match(line)
        if m:
            flush(m.group(1))

    if buf:
        flush("<unknown-package>")

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("miniflux", "v2")
class MinifluxV2(Instance):
    """Default miniflux/v2 instance - for PRs without number_interval.

    Defaults to the modules era (covers the bulk of PRs in the dataset).
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MinifluxImageDefault(
            self.pr,
            self._config,
            go_version="1.22",
            interval_name="v2",
            prepare_style="modules",
        )

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
        return miniflux_parse_log(test_log)
