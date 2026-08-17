import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class PTRImageBase(Image):
    """Repo-level base (`images/base/`, tag `:base`).

    Deliberately does NOT override `dockerfile()`: the default in
    `Image.dockerfile()` (harness/image.py:200) emits the canonical
    FROM + apt + `git clone ${REPO_URL}` + `git checkout ${BASE_COMMIT}` +
    hardening sequence, and `DockerfileEnhancer` injects TARGETARCH /
    proxy args / cert symlinks / multi-arch labels on top. Overriding
    here bypasses that and breaks multi-arch buildx / OCI export.
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

    def dependency(self) -> str:
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "envagent"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def extra_packages(self) -> list[str]:
        return []

    def files(self) -> list[File]:
        return []


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

    def dependency(self) -> "Image":
        return PTRImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "envagent"

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
                "prepare.sh",
                """#!/bin/bash
set -e

pip install -U pip
pip install -r /home/ptr/requirements.txt
pip install -e /home/ptr

# ptr_tests.test_write_stats_file_raise expects writing to /root/... to fail
# with PermissionError. Running the suite as root defeats that assertion, so
# create an unprivileged user and run tests as them.
id tester >/dev/null 2>&1 || useradd -m tester
chown -R tester:tester /home/ptr

# git operates against /home/ptr from both root (patch apply) and tester
# (test execution); mark the repo safe for both to avoid dubious-ownership refusal.
git config --global --add safe.directory /home/ptr
su tester -c 'git config --global --add safe.directory /home/ptr'

cat > /home/ptr/test_commands.sh <<'RUNNER'
#!/bin/bash
# Stale /tmp/pyproject.toml and /tmp/setup.cfg from prior root-owned runs would
# make tester's overwrite attempts fail with EACCES; clear them defensively.
rm -f /tmp/pyproject.toml /tmp/setup.cfg 2>/dev/null || true
cd /home/ptr
su tester -c 'cd /home/ptr && python ptr_tests.py -v'
RUNNER
chmod +x /home/ptr/test_commands.sh
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/ptr
bash /home/ptr/test_commands.sh
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/ptr
if ! git -C /home/ptr apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/ptr/test_commands.sh
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/ptr
if ! git -C /home/ptr apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
bash /home/ptr/test_commands.sh
""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {image_name}

{copy_commands}
RUN bash /home/prepare.sh
"""


@Instance.register("facebookincubator", "ptr")
class PTR(Instance):
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
        # unittest -v prints one line per test:
        #   "test_name (module.Class.test_name) ... VERDICT"
        # where VERDICT is `ok`, `FAIL`, `ERROR`, or `skipped 'reason'`. When
        # a test triggers a logging.debug() side effect, the debug message is
        # interleaved on the same line as `...`, pushing VERDICT to the next
        # non-empty line. Both shapes are handled below.
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        lines = log.split("\n")
        test_start_re = re.compile(r"^(test_\S+)\s+\(([^)]+)\)\s+\.\.\.\s*(.*)$")
        verdict_only_re = re.compile(r"^(ok|FAIL|ERROR|skipped)")

        i = 0
        while i < len(lines):
            m = test_start_re.match(lines[i])
            if m:
                test_id = m.group(2).strip()
                rest = m.group(3).strip()
                verdict: Optional[str] = None
                if rest:
                    for candidate in ("ok", "FAIL", "ERROR"):
                        if re.search(r"\b" + candidate + r"\s*$", rest):
                            verdict = candidate
                            break
                    if verdict is None and rest.startswith("skipped"):
                        verdict = "skipped"
                if verdict is None:
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines):
                        m2 = verdict_only_re.match(lines[j].strip())
                        if m2:
                            verdict = m2.group(1)
                if verdict == "ok":
                    passed_tests.add(test_id)
                elif verdict in ("FAIL", "ERROR"):
                    failed_tests.add(test_id)
                elif verdict and verdict.startswith("skipped"):
                    skipped_tests.add(test_id)
            i += 1

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
