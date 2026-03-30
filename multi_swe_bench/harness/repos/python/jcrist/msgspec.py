import re
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


def _parse_base_version(label: str) -> tuple[int, ...]:
    """Parse base version from label like '0.6.0..0.7.0' -> (0, 6, 0)."""
    base_ver = label.split("..")[0] if ".." in label else label
    parts = []
    for p in base_ver.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _needs_python310(pr: PullRequest) -> bool:
    """Early msgspec versions (< 0.8.0) need Python 3.10 for C API compat."""
    ver = _parse_base_version(pr.base.label)
    return ver < (0, 8, 0)


class MsgspecImageBase(Image):
    """Base image for msgspec with version-aware Python selection."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config
        self._py310 = _needs_python310(pr)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str:
        return "python:3.10-slim" if self._py310 else "python:3.11-slim"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base-py310" if self._py310 else "base-py311"

    def workdir(self) -> str:
        return "base-py310" if self._py310 else "base-py311"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        python_image = "python:3.10-slim" if self._py310 else "python:3.11-slim"
        return """
FROM {python_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
    git \\
    build-essential \\
    gcc \\
    g++ \\
    python3-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip setuptools wheel

RUN git clone https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}
""".format(pr=self.pr, python_image=python_image)


class MsgspecImageDefault(Image):
    """Per-PR image: checkout base commit, install deps, build msgspec."""

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
        return MsgspecImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """ls -la
###ACTION_DELIMITER###
cd /home/{pr.repo} && git reset --hard && git checkout {pr.base.sha}
###ACTION_DELIMITER###
pip install -e ".[dev]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
###ACTION_DELIMITER###
pip install pytest 2>/dev/null || true""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
find . -name '*.so' -path '*/msgspec/*' -delete 2>/dev/null
pip install . --no-build-isolation --force-reinstall --no-deps 2>/dev/null || pip install . --force-reinstall --no-deps 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null && pip install -e . 2>/dev/null || pip install -e . 2>/dev/null || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
find . -name '*.so' -path '*/msgspec/*' -delete 2>/dev/null
pip install . --no-build-isolation --force-reinstall --no-deps 2>/dev/null || pip install . --force-reinstall --no-deps 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null && pip install -e . 2>/dev/null || pip install -e . 2>/dev/null || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
git apply --whitespace=nowarn /home/fix.patch || git apply --whitespace=nowarn --3way /home/fix.patch || true
find . -name '*.so' -path '*/msgspec/*' -delete 2>/dev/null
pip install . --no-build-isolation --force-reinstall --no-deps 2>/dev/null || pip install . --force-reinstall --no-deps 2>/dev/null || python setup.py build_ext --inplace 2>/dev/null && pip install -e . 2>/dev/null || pip install -e . 2>/dev/null || true
pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/ 2>&1 || true
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        dep_ref = f"{dep.image_name()}:{dep.image_tag()}"

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return """
FROM {dep_ref}

WORKDIR /home/{pr.repo}
RUN git reset --hard
RUN git checkout {pr.base.sha}

RUN pip install -e ".[dev]" 2>/dev/null || pip install -e ".[test]" 2>/dev/null || pip install -e . 2>/dev/null || true
RUN pip install pytest 2>/dev/null || true

{copy_commands}
""".format(pr=self.pr, copy_commands=copy_commands, dep_ref=dep_ref)


@Instance.register("jcrist", "msgspec")
class Msgspec(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return MsgspecImageDefault(self.pr, self._config)

    _CLEAN_SO = (
        "find . -name '*.so' -path '*/msgspec/*' -delete 2>/dev/null ; "
        "find . -name '*.pyd' -path '*/msgspec/*' -delete 2>/dev/null"
    )

    _BUILD_CMD = (
        "pip install . --no-build-isolation --force-reinstall --no-deps 2>/dev/null || "
        "pip install . --force-reinstall --no-deps 2>/dev/null || "
        "python setup.py build_ext --inplace 2>/dev/null && pip install -e . 2>/dev/null || "
        "pip install -e . 2>/dev/null || true"
    )

    _PYTEST_CMD = (
        "pytest --no-header -rA --tb=no -p no:cacheprovider -v tests/"
    )

    _APPLY_OPTS = "--whitespace=nowarn"

    def run(self, run_cmd: str = "") -> str:
        if run_cmd:
            return run_cmd
        return "bash -c 'cd /home/{repo} ; {clean} ; {build} ; {pytest}'".format(
            repo=self.pr.repo,
            clean=self._CLEAN_SO,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
        )

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        if test_patch_run_cmd:
            return test_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "{clean} ; {build} ; "
            "{pytest}"
            "'"
        ).format(
            repo=self.pr.repo,
            opts=self._APPLY_OPTS,
            clean=self._CLEAN_SO,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
        )

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        if fix_patch_run_cmd:
            return fix_patch_run_cmd
        return (
            "bash -c '"
            "cd /home/{repo} ; "
            "git apply {opts} /home/test.patch || "
            "git apply {opts} --3way /home/test.patch || true ; "
            "git apply {opts} /home/fix.patch || "
            "git apply {opts} --3way /home/fix.patch || true ; "
            "{clean} ; {build} ; "
            "{pytest}"
            "'"
        ).format(
            repo=self.pr.repo,
            opts=self._APPLY_OPTS,
            clean=self._CLEAN_SO,
            build=self._BUILD_CMD,
            pytest=self._PYTEST_CMD,
        )

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*m", "", test_log)
        lines = clean_log.splitlines()

        passed_patterns = [
            re.compile(r"^(.*?)\s+PASSED\s+\[\s*\d+%\]$"),
            re.compile(r"^PASSED\s+(.*?)$"),
            re.compile(r"^(.*?)\s+PASSED$"),
        ]
        failed_patterns = [
            re.compile(r"^(.*?)\s+FAILED\s+\[\s*\d+%\]$"),
            re.compile(r"^FAILED\s+(.*?)(?: - .*)?$"),
            re.compile(r"^(.*?)\s+FAILED$"),
        ]
        error_patterns = [
            re.compile(r"^(.*?)\s+ERROR\s+\[\s*\d+%\]$"),
            re.compile(r"^ERROR\s+(.*?)$"),
        ]
        skipped_pattern = re.compile(
            r"^SKIPPED\s+(?:\[\d+\]\s+)?(tests/.*?(?:::\S+|\.py:\d+))\s*",
            re.MULTILINE,
        )

        for line in lines:
            line = line.strip()
            for pattern in passed_patterns:
                match = pattern.match(line)
                if match:
                    passed_tests.add(match.group(1).strip())
                    break
            else:
                for pattern in failed_patterns:
                    match = pattern.match(line)
                    if match:
                        failed_tests.add(match.group(1).strip())
                        break
                else:
                    for pattern in error_patterns:
                        match = pattern.match(line)
                        if match:
                            failed_tests.add(match.group(1).strip())
                            break
                    else:
                        match = skipped_pattern.match(line)
                        if match:
                            skipped_tests.add(match.group(1).strip())

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
