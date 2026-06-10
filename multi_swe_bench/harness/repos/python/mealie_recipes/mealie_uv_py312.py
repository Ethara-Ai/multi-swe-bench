import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# `uv` is a Rust binary that SIGSEGVs under QEMU x86 emulation (multi-arch
# builds on an arm64 host). So: try `uv sync` (works on native arch); if it
# crashes, fall back to a pure-Python pip install of the same dependency set
# (project + pgsql extra + the `dev` dependency-group from pyproject).
_INSTALL = (
    "uv sync --group dev --extra pgsql || ( "
    "pip install --upgrade pip setuptools wheel && "
    "pip install -e \".[pgsql]\" && "
    "python3 -c \"import tomllib; d=tomllib.load(open('pyproject.toml','rb')); "
    "g=(d.get('dependency-groups') or dict()).get('dev') or list(); "
    "print(chr(10).join(x for x in g if isinstance(x,str)))\" > /tmp/devreqs.txt && "
    "pip install -r /tmp/devreqs.txt )"
)
# Use uv's venv when present (native arch), else the system interpreter that
# the pip fallback installed into (emulated arch).
_PYSEL = (
    "if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi"
)
_PYTEST = '"$PY" -m pytest --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors'
_BIN_EXC = (
    "--exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' --exclude='*.gif' "
    "--exclude='*.ico' --exclude='*.webp' --exclude='*.bmp' --exclude='*.svg' "
    "--exclude='*.zip' --exclude='*.gz' --exclude='*.tar' --exclude='*.tgz' "
    "--exclude='*.pdf' --exclude='*.db' --exclude='*.sqlite' --exclude='*.sqlite3' "
    "--exclude='*.woff' --exclude='*.woff2' --exclude='*.ttf' --exclude='*.eot' "
    "--exclude='*.mo' --exclude='*.jar' --exclude='*.so' --exclude='*.pyc'"
)


def _apply(repo: str, patch: str) -> str:
    return (
        f"git -C /home/{repo} apply --whitespace=nowarn {_BIN_EXC} /home/{patch} "
        f"|| git -C /home/{repo} apply --whitespace=nowarn --3way {_BIN_EXC} /home/{patch} "
        f"|| ( cd /home/{repo} && patch -p1 --forward --fuzz=3 < /home/{patch} ) || true"
    )


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
        return "python:3.12-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                "ls -la\n###ACTION_DELIMITER###\n"
                "apt-get update && apt-get install -y libsasl2-dev libldap2-dev libssl-dev\n"
                "###ACTION_DELIMITER###\n"
                "pip install uv\n###ACTION_DELIMITER###\n"
                + _INSTALL
                + "\n###ACTION_DELIMITER###\n"
                + _PYSEL
                + "; "
                + _PYTEST,
            ),
            File(
                ".",
                "run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
            File(
                ".",
                "test-run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _apply(repo, "test.patch")
                + "\n"
                # Re-sync deps: patches may add dependencies to pyproject/uv.lock
                # that aren't in the base.sha environment.
                + "( " + _INSTALL + " ) || true\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
            File(
                ".",
                "fix-run.sh",
                "#!/bin/bash\n"
                f"cd /home/{repo}\n"
                + _apply(repo, "test.patch")
                + "\n"
                + _apply(repo, "fix.patch")
                + "\n"
                # Re-sync deps: the fix patch frequently adds a new dependency
                # to pyproject/uv.lock (e.g. freezegun, httpx-curl-cffi) that is
                # absent from the base.sha environment; without this the fixed
                # code fails to import and no fail->pass is observed.
                + "( " + _INSTALL + " ) || true\n"
                + _PYSEL
                + "\n"
                + _PYTEST
                + "\n\n",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile_content = (
            "\n"
            "FROM python:3.12-bookworm\n\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            "git build-essential patch libsasl2-dev libldap2-dev libssl-dev\n\n"
            "WORKDIR /home/\n"
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "RUN git clone https://github.com/mealie-recipes/mealie.git /home/mealie\n\n"
            "WORKDIR /home/mealie\n"
            "RUN git reset --hard\n"
            f"RUN git checkout {self.pr.base.sha}\n\n"
            "RUN pip install --upgrade pip && pip install uv\n"
            f"RUN {_INSTALL}\n"
        )
        dockerfile_content += f"\n{copy_commands}\n"
        return dockerfile_content


@Instance.register("mealie-recipes", "mealie_uv_py312")
class MEALIE_UV_PY312(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest `-rA` short test summary lines:
        #   PASSED tests/unit_tests/test_config.py::test_name[a b]
        #   FAILED tests/unit_tests/test_config.py::test_name - AssertionError: ...
        #   ERROR  tests/unit_tests/test_x.py::test_y - ...
        summary_pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|XFAIL|XPASS)\s+(.+?)\s*$", re.MULTILINE
        )
        for status, name in summary_pattern.findall(log):
            if status in ("FAILED", "ERROR"):
                name = re.sub(r"\s+-\s.*$", "", name).strip()
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name.strip())
            # XFAIL / XPASS: expected-fail bookkeeping, not real pass/fail

        # Grouped skip summary: SKIPPED [6] tests/unit_tests/test_x.py:18: reason
        for m in re.finditer(
            r"^SKIPPED\s+\[\d+\]\s+(\S+?):(\d+):", log, re.MULTILINE
        ):
            skipped_tests.add(f"{m.group(1)}:{m.group(2)}")

        # Defensive fallback: verbose per-test lines `nodeid STATUS [ 12%]`
        verbose_pattern = re.compile(
            r"^(.+?::.+?)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\[\s*\d+%\])?\s*$",
            re.MULTILINE,
        )
        for name, status in verbose_pattern.findall(log):
            name = name.strip()
            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
