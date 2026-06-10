import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

REPO_DIR = "khoj"

# Era: khoj v0.3 - v0.14 (171 <= PR <= 530)
# `src/khoj` package layout, pyproject.toml (hatch), requires-python >=3.8/3.9.
# Heavy ML deps (torch>=2.0.1, sentence-transformers). No database needed
# (pre-Django). Test/dev deps come from the ".[dev]" optional extra.


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
        return "python:3.11-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-v1"

    def workdir(self) -> str:
        return "base-v1"

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
    gcc g++ python3-dev libegl1 cmake \\
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
pip install --upgrade pip
pip install ".[dev]" || pip install ".[test]" || pip install . || true
# This era hard-pins fastapi==0.77.1 (pydantic v1) and openai<1.0 (uses the
# old `openai.error` API) but leaves `pydantic`/`langchain` unbounded, so pip
# drifts them to incompatible majors. Pin the contemporaneous versions:
#  - pydantic<2  (fastapi 0.77.1 needs pydantic.fields.Undefined)
#  - langchain<0.1  (0.0.x has built-in chat_models; no split langchain_community)
#  - openai<1.0  (khoj uses openai.error.* which openai>=1 removed)
#  - the ML trio must be mutually consistent at the huggingface_hub axis:
#    sentence-transformers==2.2.2 needs OLD hub (cached_download), modern
#    transformers needs NEW hub (list_repo_tree). Pin the mid-2023 set.
# Also re-install fastapi/uvicorn explicitly: tests/conftest.py imports
# `fastapi.testclient` and a silent `pip install .` failure (heavy/optional
# deps) leaves fastapi absent -> ConftestImportFailure -> 0 tests collected.
# Pin to khoj's pyproject version (pydantic-v1 era compatible).
pip install "pydantic<2" "langchain<0.1" "openai<1.0" \
    "fastapi==0.77.1" "uvicorn<0.20" \
    "huggingface_hub==0.16.4" "transformers==4.32.1" "tokenizers<0.14" || true
pip install "pytest<8" pytest-xdist freezegun factory-boy trio || true
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
# mismatch in one hunk can't discard the rest.
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


@Instance.register("khoj-ai", "khoj_171_to_530")
class KHOJ_171_TO_530(Instance):
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

        verbose_re = re.compile(
            r"^(?P<name>\S+::\S+?)\s+"
            r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
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
