from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PKG_DIFF_RE = re.compile(
    r"^diff --git a/(instrumentation/opentelemetry-instrumentation-[A-Za-z0-9_.-]+)/",
    re.MULTILINE,
)


def touched_packages(pr: PullRequest) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for patch in (pr.fix_patch, pr.test_patch):
        for m in _PKG_DIFF_RE.finditer(patch or ""):
            name = m.group(1).split("/")[-1]
            if name not in seen:
                seen.add(name)
                names.append(name)
    if not names:
        raise ValueError(
            f"opentelemetry-python-contrib: PR #{pr.number} touches no "
            "instrumentation/opentelemetry-instrumentation-* package under "
            "instrumentation/; this config only knows how to build and test "
            "that shape."
        )
    return names


def _import_module_for(package: str) -> str:
    suffix = package[len("opentelemetry-instrumentation-") :].replace("-", "_")
    return f"opentelemetry.instrumentation.{suffix}"


_FRAMEWORK_INJECT_PACKAGES = frozenset(
    {
        "opentelemetry-instrumentation-flask",
        "opentelemetry-instrumentation-django",
        "opentelemetry-instrumentation-tornado",
        "opentelemetry-instrumentation-pyramid",
        "opentelemetry-instrumentation-fastapi",
    }
)

_PACKAGE_PIP_FIXUPS: dict[str, tuple[str, ...]] = {
    "opentelemetry-instrumentation-flask": ("markupsafe<2.1", "flask<2.1", "werkzeug<2.1"),
    "opentelemetry-instrumentation-asgi": ("asgiref<3.5",),
    "opentelemetry-instrumentation-fastapi": ("httpx<0.24", "fastapi<0.90"),
    "opentelemetry-instrumentation-django": ("django<4.0",),
}

BOOTSTRAP_GEN_LOOKUP_PY = r'''import re
import sys

path, key = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
match = re.search(r'"' + re.escape(key) + r'":\s*\{\s*"library":\s*"([^"]+)"', text)
print(match.group(1) if match else "")
'''

PYTEST_FLAGS = (
    '-v --tb=short --override-ini="addopts=" -o log_cli=false -p no:cacheprovider'
)

RUN_TESTS_SH_TEMPLATE = """#!/bin/bash
PYTEST_RC=0
for pkg in {packages}
do
  echo "=== multi-swe-bench: pytest in instrumentation/$pkg ==="
  ( cd "instrumentation/$pkg" && "/home/venvs/$pkg/bin/python" -m pytest tests {flags} )
  rc=$?
  if [ "$rc" -gt "$PYTEST_RC" ]; then
    PYTEST_RC=$rc
  fi
done
exit $PYTEST_RC
"""

TEST_CMD = "bash /home/run_tests.sh"

BASE_IMAGE = "python:3.8"

CORE_REPO_URL = "https://github.com/open-telemetry/opentelemetry-python.git"
CORE_DIR = "/home/opentelemetry-python-core"

TOOLCHAIN_SETUP = r"""RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates git build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV CI=true
"""


class OpenTelemetryPythonContribImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return BASE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        if self.config.need_clone:
            return []
        return [
            File(
                ".",
                "copy_repo.sh",
                """#!/bin/bash
set -e
cp -r /home/_src_{repo} /home/{repo}
""".format(repo=self.pr.repo),
            )
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        if self.config.need_clone:
            code = f'RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/_src_{self.pr.repo}\nRUN bash /home/copy_repo.sh"

        return (
            f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

"""
            + TOOLCHAIN_SETUP
            + f"""
{copy_commands}
{code}

{self.clear_env}

CMD ["/bin/bash"]
"""
        )


class OpenTelemetryPythonContribImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._packages = touched_packages(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image:
        return OpenTelemetryPythonContribImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        packages = self._packages
        packages_sh = " ".join(packages)
        import_checks = "\n".join(
            f'    ("/home/venvs/{pkg}/bin/python", "{_import_module_for(pkg)}"),'
            for pkg in packages
        )
        package_install_lines: list[str] = [
            'BOOTSTRAP_GEN=""',
            "for candidate in \\",
            "  opentelemetry-instrumentation/src/opentelemetry/instrumentation/bootstrap_gen.py \\",
            f"  {CORE_DIR}/opentelemetry-instrumentation/src/opentelemetry/instrumentation/bootstrap_gen.py",
            "do",
            '  if [ -f "$candidate" ]; then BOOTSTRAP_GEN="$candidate"; break; fi',
            "done",
            "",
        ]
        for pkg in packages:
            suffix = pkg[len("opentelemetry-instrumentation-") :]
            venv = f"/home/venvs/{pkg}"
            pip = f'"{venv}/bin/pip"'
            package_install_lines.append(f'echo "prepare.sh: creating venv for {pkg}"')
            package_install_lines.append(f"python3 -m venv {venv}")
            package_install_lines.append(
                f"{pip} install --no-cache-dir --upgrade pip setuptools wheel -q || true"
            )
            package_install_lines.append(
                f"{pip} install --no-cache-dir {CORE_DIR}/opentelemetry-api || true"
            )
            package_install_lines.append(
                f"if [ -d {CORE_DIR}/opentelemetry-semantic-conventions ]; then "
                f"{pip} install --no-cache-dir {CORE_DIR}/opentelemetry-semantic-conventions || true; fi"
            )
            package_install_lines.append(
                f"{pip} install --no-cache-dir {CORE_DIR}/opentelemetry-sdk || true"
            )
            package_install_lines.append(
                f"if [ -d {CORE_DIR}/opentelemetry-instrumentation ]; then\n"
                f"  {pip} install --no-cache-dir {CORE_DIR}/opentelemetry-instrumentation || true\n"
                "elif [ -d opentelemetry-instrumentation ]; then\n"
                f"  {pip} install --no-cache-dir ./opentelemetry-instrumentation || true\n"
                "fi"
            )
            package_install_lines.append(
                f"if [ -d {CORE_DIR}/tests/opentelemetry-test-utils ]; then\n"
                f"  {pip} install --no-cache-dir {CORE_DIR}/tests/opentelemetry-test-utils || true\n"
                f"elif [ -d {CORE_DIR}/tests/util ]; then\n"
                f"  {pip} install --no-cache-dir {CORE_DIR}/tests/util || true\n"
                "fi"
            )
            package_install_lines.append(
                "if [ -d util/opentelemetry-util-http ]; then\n"
                f'  {pip} install --no-cache-dir -e "util/opentelemetry-util-http[test]" || true\n'
                "fi"
            )
            if pkg in _FRAMEWORK_INJECT_PACKAGES:
                package_install_lines.append(
                    "if [ -n \"$BOOTSTRAP_GEN\" ]; then\n"
                    f'  SPEC=$(python3 /home/bootstrap_gen_lookup.py "$BOOTSTRAP_GEN" "{suffix}")\n'
                    "  if [ -n \"$SPEC\" ]; then\n"
                    f'    echo "prepare.sh: installing framework for {pkg} -> $SPEC"\n'
                    f'    {pip} install --no-cache-dir "$SPEC" || true\n'
                    "  fi\n"
                    "fi"
                )
            package_install_lines.append(
                "for sib in $(grep -ohE \"opentelemetry-instrumentation-[a-zA-Z0-9_-]+\" "
                f"instrumentation/{pkg}/setup.cfg instrumentation/{pkg}/pyproject.toml "
                "2>/dev/null | sort -u); do\n"
                f'  if [ "$sib" != "{pkg}" ] && [ -d "instrumentation/$sib" ]; then\n'
                f'    echo "prepare.sh: installing sibling dependency $sib for {pkg}"\n'
                f'    {pip} install --no-cache-dir -e "instrumentation/$sib" || true\n'
                "  fi\n"
                "done"
            )
            package_install_lines.append(
                f'{pip} install --no-cache-dir -e "instrumentation/{pkg}[test]" || true'
            )
            fixups = _PACKAGE_PIP_FIXUPS.get(pkg)
            if fixups:
                specs = " ".join(f'"{spec}"' for spec in fixups)
                package_install_lines.append(
                    f"{pip} install --no-cache-dir {specs} || true"
                )
            package_install_lines.append(
                f'{pip} install --no-cache-dir "pytest==7.4.4" || true'
            )
        package_installs = "\n".join(package_install_lines)
        run_tests_sh = RUN_TESTS_SH_TEMPLATE.format(
            packages=packages_sh, flags=PYTEST_FLAGS
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "bootstrap_gen_lookup.py", BOOTSTRAP_GEN_LOOKUP_PY),
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

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

CORE_SHA=$(grep -m1 -oE 'CORE_REPO_SHA:[[:space:]]*[0-9a-f]{{40}}' \\
    .github/workflows/test.yml | grep -oE '[0-9a-f]{{40}}')

if [ -z "$CORE_SHA" ]; then
  echo "prepare.sh: FATAL -- could not read CORE_REPO_SHA from" >&2
  echo "prepare.sh: .github/workflows/test.yml. The core repo revision is not" >&2
  echo "prepare.sh: guessable; refusing to continue with a wrong one." >&2
  exit 1
fi
echo "prepare.sh: core repo pinned by this commit -> $CORE_SHA"

mkdir -p {core_dir}
cd {core_dir}
git init -q
git remote add origin {core_url}
git fetch -q --depth 1 origin "$CORE_SHA"
git checkout -q FETCH_HEAD
cd /home/{pr.repo}

{package_installs}

python3 - <<'PYCHECK'
import subprocess
import sys

missing = []
for python_bin, mod in (
{import_checks}
):
    proc = subprocess.run(
        [python_bin, "-c", f"import {{mod}}"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        missing.append(f"{{python_bin}}: {{mod}} ({{proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'import failed'}})")
if missing:
    print("prepare.sh: FATAL -- environment incomplete:", file=sys.stderr)
    for m in missing:
        print("  " + m, file=sys.stderr)
    sys.exit(1)
print("prepare.sh: all required modules import cleanly")
PYCHECK

git checkout -- .
git clean -fd
bash /home/check_git_changes.sh

if [ "$(uname -m)" = "x86_64" ]; then
  set +e
  {test_cmd} > /tmp/warmup.log 2>&1
  WARMUP_RC=$?
  set -e
  tail -40 /tmp/warmup.log

  if [ "$WARMUP_RC" -ge 2 ]; then
    echo "prepare.sh: FATAL -- pytest could not run the suite (exit $WARMUP_RC)." >&2
    tail -40 /tmp/warmup.log >&2
    exit 1
  fi
else
  echo "prepare.sh: $(uname -m) is not the grading architecture -- skipping the"
  echo "prepare.sh: test warm-up."
fi

""".format(
                    pr=self.pr,
                    test_cmd=TEST_CMD,
                    core_url=CORE_REPO_URL,
                    core_dir=CORE_DIR,
                    import_checks=import_checks,
                    package_installs=package_installs,
                ),
            ),
            File(
                ".",
                "run_tests.sh",
                run_tests_sh,
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}

""".format(pr=self.pr, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name() # type: ignore
        tag = image.image_tag() # type: ignore
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}

"""


_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_RE_INLINE = re.compile(
    r"^(\S+\.py::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_RE_SUMMARY = re.compile(r"^(?:FAILED|ERROR)\s+(\S+\.py::\S+?)(?:\s|$)")
_RE_COLLECT_ERROR = re.compile(r"^ERROR\s+(\S+\.py)\s*$")

KNOWN_FLAKY_TESTS: frozenset[str] = frozenset()


def parse_pytest_log(log: str) -> TestResult:
    log = _RE_ANSI.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    def record(status: str, test_id: str) -> None:
        if test_id in KNOWN_FLAKY_TESTS:
            return
        if status in ("PASSED", "XPASS"):
            if test_id in failed_tests:
                return
            skipped_tests.discard(test_id)
            passed_tests.add(test_id)
        elif status in ("FAILED", "ERROR"):
            passed_tests.discard(test_id)
            skipped_tests.discard(test_id)
            failed_tests.add(test_id)
        elif status in ("SKIPPED", "XFAIL"):
            if test_id not in passed_tests and test_id not in failed_tests:
                skipped_tests.add(test_id)

    for line in log.splitlines():
        line = line.rstrip()

        match = _RE_INLINE.match(line)
        if match:
            record(match.group(2), match.group(1))
            continue

        match = _RE_SUMMARY.match(line)
        if match:
            record("FAILED", match.group(1))
            continue

        match = _RE_COLLECT_ERROR.match(line)
        if match:
            record("FAILED", f"{match.group(1)}::[collection error]")
            continue

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("open-telemetry", "opentelemetry-python-contrib")
class OpenTelemetryPythonContrib(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
        return OpenTelemetryPythonContribImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return parse_pytest_log(test_log)
