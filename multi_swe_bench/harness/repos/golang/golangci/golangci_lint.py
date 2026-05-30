import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GolangciLintVersionBase(Image):
    """Version-bucketed base image: FROM golang:{version} + clone repo.

    Shared across all PRs in the same Go version bucket.
    image_tag = "base-{interval_name}" (e.g., "base-golangci-lint_558_to_0").
    """

    def __init__(self, pr: PullRequest, config: Config, go_version: str, interval_name: str = ""):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name or f"go{go_version}"

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    @property
    def go_version(self) -> str:
        return self._go_version

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

        debian_fix = ""
        if self._go_version in ("1.13", "1.14", "1.15", "1.16", "1.17", "1.18", "1.19", "1.20"):
            debian_fix = (
                "RUN if grep -q 'buster\\|stretch' /etc/apt/sources.list 2>/dev/null; then \\\n"
                "        sed -i 's|deb.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
                "        sed -i 's|security.debian.org|archive.debian.org|g' /etc/apt/sources.list && \\\n"
                "        sed -i '/stretch-updates/d' /etc/apt/sources.list && \\\n"
                "        sed -i '/buster-updates/d' /etc/apt/sources.list && \\\n"
                "        echo 'Acquire::Check-Valid-Until \"false\";' > /etc/apt/apt.conf.d/99no-check-valid; \\\n"
                "    fi\n"
            )

        global_env = self.global_env.strip() if isinstance(self.global_env, str) else ""
        clear_env = self.clear_env.strip() if isinstance(self.clear_env, str) else ""

        # DockerfileEnhancer injects a cert/proxy infra block + a blank line
        # right after the FROM line, then extends with our remaining lines.
        # If our first post-FROM line is itself blank, we'd end up with two
        # consecutive blanks. Emit our remaining sections with no leading
        # blank — the enhancer's own spacing handles separation from infra.
        remaining = []
        if debian_fix:
            remaining.append(debian_fix.rstrip())
        remaining.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends git "
            "&& rm -rf /var/lib/apt/lists/* || true"
        )
        if global_env:
            remaining.append(global_env)
        remaining.append("WORKDIR /home/")
        remaining.append(code)
        if clear_env:
            remaining.append(clear_env)
        return f"FROM {image_name}\n" + "\n\n".join(remaining) + "\n"


_COMMON_ENV = """export CI=true
export GOTOOLCHAIN=auto
export GL_TEST_RUN=1
export GOLANGCI_LINT_INSTALLED=true
export CGO_ENABLED=1
export GO111MODULE=auto"""


_GOPATH_SETUP = """# Pre-modules era: stage the repo under GOPATH so Go can resolve imports.
export GOPATH=/go
REPO_PATH="$GOPATH/src/github.com/{pr.org}/{pr.repo}"
mkdir -p "$(dirname "$REPO_PATH")"
if [ ! -e "$REPO_PATH" ]; then
    ln -s /home/{pr.repo} "$REPO_PATH"
fi
cd "$REPO_PATH" """


class GolangciLintImageDefault(Image):
    """Per-PR image: FROM version-base -> checkout + patches + prepare.

    prepare_style:
      - "modules" (default): cd into clone, go mod download, go test ./...
      - "gopath":  pre-modules era; symlink into $GOPATH/src/<org>/<repo>, use vendor/
    """

    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        go_version: str = "1.25",
        interval_name: str = "",
        prepare_style: str = "modules",
    ):
        self._pr = pr
        self._config = config
        self._go_version = go_version
        self._interval_name = interval_name or f"go{go_version}"
        self._prepare_style = prepare_style

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return GolangciLintVersionBase(self.pr, self.config, self._go_version, self._interval_name)

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

# Vendor-mode short-circuits the network and skips modern Go's strict
# pseudo-version validation that rejects pre-2019 go.mod entries.
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi

go mod tidy 2>&1 || echo "go mod tidy failed (non-fatal)"
go mod download 2>&1 || echo "go mod download failed (non-fatal)"

if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi

go test -v -count=1 -timeout 20m ./... || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

cd /home/{self.pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
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

# Initial checkout uses the real clone path (where git history lives).
cd /home/{self.pr.repo}
git reset --hard
git checkout {self.pr.base.sha}

{gopath_setup}

# Pre-modules golangci-lint ships its deps under vendor/.
# Make sure GOFLAGS uses vendor mode when present.
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi

if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi

go test -v -count=1 -timeout 20m ./... || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

git apply --whitespace=nowarn /home/test.patch || {{ echo "Warning: git apply test.patch failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

{env}

{gopath_setup}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ echo "Warning: git apply failed, retrying with --reject..."; git apply --reject --whitespace=nowarn /home/test.patch 2>&1 || true; git apply --reject --whitespace=nowarn /home/fix.patch 2>&1 || true; find . -name '*.rej' -delete 2>/dev/null || true; }}
if [ -d ./vendor ]; then
    export GOFLAGS="-mod=vendor"
fi
if [ -d ./cmd/golangci-lint ]; then
    go build -o golangci-lint ./cmd/golangci-lint 2>&1 || echo "go build failed (non-fatal)"
fi
go test -v -count=1 -timeout 20m ./...
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

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_PASS = re.compile(r"--- PASS: (\S+)")
_RE_FAIL = re.compile(r"--- FAIL: (\S+)")
_RE_SKIP = re.compile(r"--- SKIP: (\S+)")
# Package summary lines produced by `go test`:
#   "ok      github.com/foo/bar/pkg  0.123s"
#   "FAIL    github.com/foo/bar/pkg  0.123s"
#   "?       github.com/foo/bar/pkg  [no test files]"
_RE_PKG_OK = re.compile(r"^ok\s+(\S+)")
_RE_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)")
_RE_PKG_NOTEST = re.compile(r"^\?\s+(\S+)")


def golangci_lint_parse_log(test_log: str) -> TestResult:
    """Shared parse_log for all golangci-lint instances.

    Go subtest names (e.g. `TestX/case1`) are not globally unique — the same
    name can appear in multiple packages. To avoid collisions, we buffer test
    names per package and prefix each one with the package path emitted on the
    trailing `ok|FAIL pkg` summary line.
    """
    test_log = _ANSI_RE.sub("", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Buffer test outcomes for the current package; flush when we see `ok pkg`,
    # `FAIL pkg`, or `? pkg`. Buffer holds (status, raw_name) tuples.
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

    # Trailing tests with no package summary (rare — `go test` crashed mid-run).
    # Flush under a synthetic package so they're still counted.
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


@Instance.register("golangci", "golangci-lint")
class GolangciLint(Instance):
    """Default golangci-lint instance - for PRs without number_interval."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GolangciLintImageDefault(
            self.pr,
            self._config,
            go_version="1.25",
            interval_name="golangci-lint",
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
        return golangci_lint_parse_log(test_log)
