import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _extract_test_files(test_patch: str) -> dict[str, list[str]]:
    """Parse .test.ts/.spec.ts paths from diff headers, grouped by package dir.

    Returns e.g. ``{"packages/core": ["src/tools/builder.test.ts"]}``.
    Keys are the first two path components; values are paths relative to that dir.
    """
    seen: set[str] = set()
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", test_patch, re.MULTILINE):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        if ".test." in path or ".spec." in path:
            files.append(path)

    grouped: dict[str, list[str]] = {}
    for f in files:
        parts = f.split("/")
        if len(parts) >= 3:
            pkg = "/".join(parts[:2])
            relative = "/".join(parts[2:])
        else:
            pkg = "."
            relative = f
        grouped.setdefault(pkg, []).append(relative)
    return grouped


def _build_vitest_commands(repo: str, test_patch: str) -> str:
    """Build per-package vitest commands so workspace mode doesn't run all packages."""
    grouped = _extract_test_files(test_patch)
    if not grouped:
        return "pnpm vitest run --reporter=verbose 2>&1 || true"

    lines: list[str] = []
    for pkg_dir, rel_files in grouped.items():
        files_str = " ".join(rel_files)
        lines.append(
            "pnpm --filter './{pkg}' exec vitest run --reporter=verbose {files} 2>&1 || true".format(
                pkg=pkg_dir, files=files_str
            )
        )
    return "\n".join(lines)


def _build_turbo_commands(test_patch: str) -> str:
    """turbo build --filter auto-resolves workspace deps in topological order."""
    grouped = _extract_test_files(test_patch)
    if not grouped:
        return ""

    lines: list[str] = []
    for pkg_dir in grouped:
        lines.append(
            "pnpm turbo run build --filter='./{pkg}' 2>&1 || true".format(
                pkg=pkg_dir
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Shared parse_log for vitest output
# ---------------------------------------------------------------------------


def mastra_parse_log(test_log: str) -> TestResult:
    """Parse Vitest test output for mastra-ai/mastra.

    Handles:
    - Vitest verbose: checkmark/cross individual tests
    - Vitest file-level PASS/FAIL
    - Vitest summary lines
    """
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    # Vitest patterns
    re_vitest_pass = re.compile(
        r"^\s*[✓✔]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_fail = re.compile(
        r"^\s*[×✗]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    re_vitest_skip = re.compile(
        r"^\s*[-↓○]\s+(.+?)(?:\s+\d+[\.\d]*\s*(?:ms|s))?\s*$"
    )
    # Vitest file-level FAIL
    re_vitest_fail_file = re.compile(
        r"^\s*FAIL\s+(\S+\.(?:test|spec)\.(?:ts|tsx|js|jsx|mts|mjs))"
    )
    # Vitest summary: "Test Files  N passed (N)"
    re_vitest_summary_passed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+passed"
    )
    re_vitest_summary_failed = re.compile(
        r"^\s*Test Files\s+(\d+)\s+failed"
    )

    for line in test_log.splitlines():
        line = ansi_escape.sub("", line).strip()
        if not line:
            continue

        # Vitest pass/fail/skip
        m = re_vitest_pass.match(line)
        if m:
            name = m.group(1).strip()
            if name not in failed_tests:
                passed_tests.add(name)
            continue

        m = re_vitest_fail.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_skip.match(line)
        if m:
            skipped_tests.add(m.group(1).strip())
            continue

        m = re_vitest_fail_file.match(line)
        if m:
            name = m.group(1).strip()
            failed_tests.add(name)
            passed_tests.discard(name)
            continue

        m = re_vitest_summary_passed.match(line)
        if m and not passed_tests:
            passed_tests.add("__vitest_suite_passed__")
            continue

        m = re_vitest_summary_failed.match(line)
        if m and not failed_tests:
            failed_tests.add("__vitest_suite_failed__")
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


# ---------------------------------------------------------------------------
# Shared shell script templates
# ---------------------------------------------------------------------------

_PREPARE_SH = """#!/bin/bash
set -e

# Install correct pnpm version for this PR's SHA
npm install -g pnpm@{pnpm_version}

cd /home/{repo}
git reset --hard
git checkout {base_sha}

# Install dependencies
pnpm install --no-frozen-lockfile 2>&1 || pnpm install 2>&1 || true

# Build tested packages and their workspace dependencies
{build_commands}
"""

_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
{vitest_commands}
"""

_TEST_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --reject /home/test.patch || true
{vitest_commands}
"""

_FIX_RUN_SH = """#!/bin/bash
set -e
cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || {{ git apply --whitespace=nowarn --reject /home/test.patch || true; git apply --whitespace=nowarn --reject /home/fix.patch || true; }}
pnpm install --no-frozen-lockfile 2>&1 || true
{build_commands}
{vitest_commands}
"""

_NODE_IMAGE = "node:20"


# ---------------------------------------------------------------------------
# Single shared base image — clones repo, installs git/jq.
# pnpm version is NOT in the base; it's installed in prepare.sh per-PR.
# All 71 PRs share this one base image (image_tag="base").
# ---------------------------------------------------------------------------


class MastraImageBase(Image):
    """Shared base image — node:20, git, jq, repo clone. No pnpm."""

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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = "RUN git clone https://github.com/{org}/{repo}.git /home/{repo}".format(
                org=self.pr.org, repo=self.pr.repo
            )
        else:
            code = "COPY {repo} /home/{repo}".format(repo=self.pr.repo)

        return """FROM {image_name}

{global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y --no-install-recommends git jq && rm -rf /var/lib/apt/lists/*

{code}

{clear_env}

""".format(
            image_name=image_name,
            global_env=self.global_env,
            code=code,
            clear_env=self.clear_env,
        )


# ---------------------------------------------------------------------------
# Per-PR image helpers
# ---------------------------------------------------------------------------


def _make_pr_files(pr: PullRequest, pnpm_version: str) -> list[File]:
    """Generate shell scripts and patch files for a PR image."""
    vitest_commands = _build_vitest_commands(pr.repo, pr.test_patch)
    build_commands = _build_turbo_commands(pr.test_patch)
    return [
        File(".", "fix.patch", pr.fix_patch),
        File(".", "test.patch", pr.test_patch),
        File(
            ".",
            "prepare.sh",
            _PREPARE_SH.format(
                repo=pr.repo, base_sha=pr.base.sha,
                build_commands=build_commands,
                pnpm_version=pnpm_version,
            ),
        ),
        File(".", "run.sh", _RUN_SH.format(repo=pr.repo, vitest_commands=vitest_commands)),
        File(".", "test-run.sh", _TEST_RUN_SH.format(repo=pr.repo, vitest_commands=vitest_commands)),
        File(".", "fix-run.sh", _FIX_RUN_SH.format(
            repo=pr.repo,
            vitest_commands=vitest_commands,
            build_commands=build_commands,
        )),
    ]


def _make_pr_dockerfile(image: Image, pnpm_version: str) -> str:
    """Generate multi-stage Dockerfile for a per-PR image.

    Uses cross-compilation: the build stage runs on the native platform
    (no QEMU emulation) so Go-based tools like esbuild don't crash.
    The final stage targets each requested platform and re-installs native
    node modules for that platform while reusing the JS build artifacts.
    """
    dep = image.dependency()
    name = dep.image_name()
    tag = dep.image_tag()

    copy_commands = ""
    for file in image.files():
        copy_commands += "COPY {name} /home/\n".format(name=file.name)

    return """# syntax=docker/dockerfile:1.6
FROM --platform=$BUILDPLATFORM {name}:{tag} AS builder

{global_env}

{copy_commands}

RUN bash /home/prepare.sh

FROM {name}:{tag}

{global_env}

COPY --from=builder /home/{repo} /home/{repo}

RUN npm install -g pnpm@{pnpm_version} && \\
    cd /home/{repo} && \\
    (pnpm install --no-frozen-lockfile 2>&1 || pnpm install 2>&1 || true)

{copy_commands}

{clear_env}

""".format(
        name=name,
        tag=tag,
        repo=image.pr.repo,
        pnpm_version=pnpm_version,
        global_env=image.global_env,
        copy_commands=copy_commands,
        clear_env=image.clear_env,
    )


# ---------------------------------------------------------------------------
# Default Mastra Instance (fallback for PRs without number_interval)
# ---------------------------------------------------------------------------

_DEFAULT_PNPM_VERSION = "10.18.2"


class MastraImageDefault(Image):
    """Default per-PR image (no number_interval fallback)."""

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
        return MastraImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def workdir(self) -> str:
        return "pr-{number}".format(number=self.pr.number)

    def files(self) -> list[File]:
        return _make_pr_files(self.pr, _DEFAULT_PNPM_VERSION)

    def dockerfile(self) -> str:
        return _make_pr_dockerfile(self, _DEFAULT_PNPM_VERSION)


@Instance.register("mastra-ai", "mastra")
class Mastra(Instance):
    """Default instance for mastra-ai/mastra (no number_interval)."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MastraImageDefault(self.pr, self._config)

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
        return mastra_parse_log(test_log)


# ---------------------------------------------------------------------------
# Interval-specific instance factory
# All intervals share MastraImageBase (one base image).
# pnpm version is baked into prepare.sh, not the base image.
# ---------------------------------------------------------------------------


def _make_interval_image_default(pnpm_version: str):
    """Create an ImageDefault subclass bound to a specific pnpm version."""

    class _ImageDefault(Image):

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
            return MastraImageBase(self.pr, self._config)

        def image_tag(self) -> str:
            return "pr-{number}".format(number=self.pr.number)

        def workdir(self) -> str:
            return "pr-{number}".format(number=self.pr.number)

        def files(self) -> list[File]:
            return _make_pr_files(self.pr, pnpm_version)

        def dockerfile(self) -> str:
            return _make_pr_dockerfile(self, pnpm_version)

    return _ImageDefault


def _register_interval(interval_name: str, pnpm_version: str):
    """Create and register an Instance subclass for a specific interval."""

    image_cls = _make_interval_image_default(pnpm_version)

    @Instance.register("mastra-ai", interval_name)
    class _Interval(Instance):

        def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
            super().__init__()
            self._pr = pr
            self._config = config

        @property
        def pr(self) -> PullRequest:
            return self._pr

        def dependency(self) -> Optional[Image]:
            return image_cls(self.pr, self._config)

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
            return mastra_parse_log(test_log)

    class_name = "MASTRA_{interval}".format(
        interval=interval_name.upper().replace("MASTRA_", "")
    )
    _Interval.__name__ = class_name
    _Interval.__qualname__ = class_name
    return _Interval


# ---------------------------------------------------------------------------
# All 43 intervals — (interval_name, pnpm_version)
# ---------------------------------------------------------------------------

_INTERVALS = [
    ("mastra_3324_to_3324", "10.10.0"),
    ("mastra_3758_to_3642", "10.8.0"),
    ("mastra_3841_to_3841", "10.10.0"),
    ("mastra_5511_to_5511", "10.12.4"),
    ("mastra_5669_to_5669", "10.10.0"),
    ("mastra_5936_to_5936", "10.12.4"),
    ("mastra_6075_to_6026", "10.12.4"),
    ("mastra_6086_to_6086", "10.12.4"),
    ("mastra_6107_to_6107", "10.12.4"),
    ("mastra_6154_to_6109", "10.12.4"),
    ("mastra_6180_to_6180", "10.12.4"),
    ("mastra_6398_to_6398", "10.12.4"),
    ("mastra_6466_to_6441", "10.12.4"),
    ("mastra_6595_to_6494", "10.12.4"),
    ("mastra_6612_to_6612", "10.12.4"),
    ("mastra_6675_to_6675", "10.12.4"),
    ("mastra_6677_to_6677", "10.12.4"),
    ("mastra_6801_to_6801", "10.12.4"),
    ("mastra_6845_to_6845", "10.12.4"),
    ("mastra_6862_to_6862", "10.12.4"),
    ("mastra_7349_to_7349", "10.12.4"),
    ("mastra_7539_to_7539", "10.12.4"),
    ("mastra_7606_to_7606", "10.12.4"),
    ("mastra_7987_to_7987", "10.12.4"),
    ("mastra_8103_to_8103", "10.12.4"),
    ("mastra_8603_to_8603", "10.12.4"),
    ("mastra_9080_to_9069", "10.18.2"),
    ("mastra_9349_to_9349", "10.18.2"),
    ("mastra_9409_to_9409", "10.18.2"),
    ("mastra_9472_to_9472", "10.18.2"),
    ("mastra_10043_to_10043", "10.18.2"),
    ("mastra_10487_to_10244", "10.18.2"),
    ("mastra_10564_to_10558", "10.18.2"),
    ("mastra_10741_to_10637", "10.18.2"),
    ("mastra_10964_to_10850", "10.18.2"),
    ("mastra_11305_to_11305", "10.18.2"),
    ("mastra_11311_to_11311", "10.18.2"),
    ("mastra_11445_to_11445", "10.18.2"),
    ("mastra_11832_to_11832", "10.18.2"),
    ("mastra_11842_to_11833", "10.18.2"),
    ("mastra_11905_to_11905", "10.18.2"),
    ("mastra_11944_to_11939", "10.18.2"),
    ("mastra_12815_to_12815", "10.29.3"),
]

# Register all intervals at import time
for _interval_name, _pnpm_version in _INTERVALS:
    _register_interval(_interval_name, _pnpm_version)
