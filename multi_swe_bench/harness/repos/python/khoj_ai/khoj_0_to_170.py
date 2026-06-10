import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "khoj"

# Era: khoj v0.1.x - v0.2.x (PR <= 170)
# Flat `src/` layout, packaged via setup.py (python_requires ">=3.8, <3.11").
# Heavy ML deps (torch==1.13.1, sentence-transformers==2.1.0). pyqt6 is a
# desktop-GUI dependency that fails to build headless and is not needed for
# the test suite, so it is stripped from setup.py before install.


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

    def dependency(self) -> Union[str, "Image"]:
        return "python:3.10-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-v0"

    def workdir(self) -> str:
        return "base-v0"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{REPO_DIR}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{REPO_DIR}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg make sudo wget build-essential \\
    gcc g++ python3-dev libegl1 \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{REPO_DIR}
RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_prefix(self) -> str:
        return "mswebench"

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
                f"""#!/bin/bash
set -e
cd /home/{REPO_DIR}
git reset --hard
bash /home/check_git_changes.sh
git checkout {self.pr.base.sha}
bash /home/check_git_changes.sh
# pyqt6 is a desktop-GUI only dep that fails to build headless; not used by tests
sed -i '/pyqt6/d' setup.py
pip install --upgrade pip
pip install . || true
# setup.py pins pytest==7.1.2; keep pytest<8 (avoid pytest 9 API breaks)
pip install "pytest<8" || true
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
export CI=true
export HF_HUB_DISABLE_XET=1
export TOKENIZERS_PARALLELISM=false
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
export CI=true
export HF_HUB_DISABLE_XET=1
export TOKENIZERS_PARALLELISM=false
# Strip unappliable binary file-diffs, then --reject so a context
# mismatch (e.g. the build-time sed edit to setup.py) in one hunk
# can't discard the rest.
for p in /home/test.patch; do
  python - "$p" "$p.nobin" <<'PYEOF' || cp "$p" "$p.nobin"
import sys
src = sys.argv[1]
dst = sys.argv[2]
data = open(src, errors="replace").read().split("\n")
out = []
block = []
is_bin = False
for ln in data:
    if ln.startswith("diff --git "):
        if block and not is_bin:
            out.extend(block)
        block = [ln]
        is_bin = False
    else:
        block.append(ln)
        if ln.startswith("GIT binary patch") or ln.startswith("Binary files "):
            is_bin = True
if block and not is_bin:
    out.extend(block)
open(dst, "w").write("\n".join(out))
PYEOF
  git apply --whitespace=nowarn "$p.nobin" \
    || git apply --whitespace=nowarn --reject "$p.nobin" \
    || patch -p1 -f --no-backup-if-mismatch < "$p.nobin" \
    || true
done
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
cd /home/{REPO_DIR}
export CI=true
export HF_HUB_DISABLE_XET=1
export TOKENIZERS_PARALLELISM=false
# Apply test then fix patch separately. Strip unappliable binary
# file-diffs first, then --reject so one bad hunk can't discard the rest.
for p in /home/test.patch /home/fix.patch; do
  python - "$p" "$p.nobin" <<'PYEOF' || cp "$p" "$p.nobin"
import sys
src = sys.argv[1]
dst = sys.argv[2]
data = open(src, errors="replace").read().split("\n")
out = []
block = []
is_bin = False
for ln in data:
    if ln.startswith("diff --git "):
        if block and not is_bin:
            out.extend(block)
        block = [ln]
        is_bin = False
    else:
        block.append(ln)
        if ln.startswith("GIT binary patch") or ln.startswith("Binary files "):
            is_bin = True
if block and not is_bin:
    out.extend(block)
open(dst, "w").write("\n".join(out))
PYEOF
  git apply --whitespace=nowarn "$p.nobin" \
    || git apply --whitespace=nowarn --reject "$p.nobin" \
    || patch -p1 -f --no-backup-if-mismatch < "$p.nobin" \
    || true
done
python -m pytest -v -rA -p no:cacheprovider --continue-on-collection-errors
""",
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

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("khoj-ai", "khoj_0_to_170")
class KHOJ_0_TO_170(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        # Strip ANSI escape codes first
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # pytest -v line format: "tests/test_x.py::test_y[param] PASSED [ 12%]"
        verbose_re = re.compile(
            r"^(?P<name>\S+::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        # pytest -rA summary lines: "PASSED tests/test_x.py::test_y"
        summary_re = re.compile(
            r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<name>\S+::\S+)"
        )

        for raw in clean_log.splitlines():
            line = raw.strip()
            m = verbose_re.match(line)
            if m:
                name, status = m.group("name"), m.group("status")
            else:
                m = summary_re.match(line)
                if not m:
                    continue
                name, status = m.group("name"), m.group("status")

            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR", "XFAIL"):
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)

        # Enforce TestResult invariants (worst status wins)
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
