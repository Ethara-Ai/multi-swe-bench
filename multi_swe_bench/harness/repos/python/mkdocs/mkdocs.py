import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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
        return "python:3.8"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        base_sha = self.pr.base.sha[:8] if hasattr(self.pr.base, "sha") else "base"
        return f"base-{base_sha}"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def extra_setup(self) -> str:
        return (
            "RUN pip install --no-cache-dir "
            "'jinja2<3.1' 'markupsafe<2.1' 'Markdown<3.4' 'click<8' mock\n"
            "RUN pip install --no-cache-dir -e ."
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

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
                "run.sh",
                """#!/bin/bash
cd /home/{repo}
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "strip_binary.sh",
                r"""#!/bin/bash
# Strip binary diff hunks from a patch file, outputting text-only diffs.
# A text diff always has @@ hunk markers. Binary diffs don't.
awk '
/^diff --git / {
    if (buf != "" && has_hunk) { printf "%s", buf }
    buf = $0 "\n"; has_hunk = 0; next
}
/^@@/ { has_hunk = 1 }
{ buf = buf $0 "\n" }
END { if (buf != "" && has_hunk) printf "%s", buf }
' "$@"
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /tmp/test_text.patch
if [ -s /tmp/test_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/test_text.patch; then
        echo "Error: git apply failed for test patch" >&2
        exit 1
    fi
    # Re-install if setup.py or pyproject.toml changed
    if grep -q "setup.py\|setup.cfg\|pyproject.toml" /tmp/test_text.patch; then
        pip install --no-cache-dir -e . > /dev/null 2>&1
    fi
fi
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{repo}
bash /home/strip_binary.sh /home/test.patch > /tmp/test_text.patch
bash /home/strip_binary.sh /home/fix.patch > /tmp/fix_text.patch
if [ -s /tmp/test_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/test_text.patch; then
        echo "Error: git apply failed for test patch" >&2
        exit 1
    fi
fi
if [ -s /tmp/fix_text.patch ]; then
    if ! git -C /home/{repo} apply --whitespace=nowarn /tmp/fix_text.patch; then
        echo "Error: git apply failed for fix patch" >&2
        exit 1
    fi
fi
# Re-install if any patch touched setup.py/pyproject.toml (new deps)
if grep -q "setup.py\|setup.cfg\|pyproject.toml" /tmp/test_text.patch /tmp/fix_text.patch 2>/dev/null; then
    pip install --no-cache-dir -e . > /dev/null 2>&1
fi
python -m unittest discover -s mkdocs -p '*tests.py' -v 2>&1

""".format(repo=self.pr.repo),
            ),
        ]

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

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("mkdocs", "mkdocs")
class Mkdocs(Instance):
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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # unittest verbose output format:
        # test_name (module.Class.test_name) ... ok
        # test_name (module.Class.test_name) ... FAIL
        # test_name (module.Class.test_name) ... ERROR
        # test_name (module.Class.test_name) ... skipped 'reason'
        pattern = re.compile(
            r"^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped\b)",
            re.MULTILINE,
        )

        for match in pattern.finditer(log):
            test_method = match.group(1)
            test_class = match.group(2)
            test_id = f"{test_class}.{test_method}"
            status = match.group(3)

            if status == "ok":
                passed_tests.add(test_id)
            elif status in ("FAIL", "ERROR"):
                failed_tests.add(test_id)
            elif status.startswith("skipped"):
                skipped_tests.add(test_id)

        passed_tests -= failed_tests | skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
