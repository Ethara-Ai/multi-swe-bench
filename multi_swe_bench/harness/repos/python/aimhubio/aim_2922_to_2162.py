import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class AimImageBase(Image):
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
        return "python:3.10-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip wheel 'setuptools<81'

RUN python -m pip install --no-cache-dir \\
    'numpy==1.23.5' \\
    'protobuf==3.20.3' \\
    'SQLAlchemy==1.4.48' \\
    'Pillow==9.5.0' \\
    'alembic==1.10.4' \\
    'uvicorn==0.22.0' \\
    'pytest==7.3.1' \\
    'parameterized==0.8.1'

RUN python -m pip install --no-cache-dir \\
    'tensorflow==2.12.0' \\
    'torch==2.0.1'

{code}

{copy_commands}

{self.clear_env}

"""


class AimImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return AimImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        if self.pr.number <= 2477:
            toolchain = """python -m pip install --no-cache-dir \\
    'Cython @ https://github.com/cython/cython/archive/refs/tags/3.0.0a9.tar.gz' || true
python -m pip install --no-cache-dir \\
    'aimrocks==0.2.1' \\
    'fastapi==0.67.0' \\
    'starlette==0.14.2' \\
    'pydantic==1.9.2' \\
    'requests==2.31.0' \\
    'pandas==1.5.3' || true"""
        else:
            toolchain = """python -m pip install --no-cache-dir 'Cython==3.0.0b1' || true
python -m pip install --no-cache-dir \\
    'aimrocks==0.4.0' \\
    'fastapi==0.95.2' \\
    'starlette==0.27.0' \\
    'pydantic==1.10.13' \\
    'httpx==0.24.1' \\
    'pandas==2.0.1' || true"""

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

cd /home
if ! git -C /home/{pr.repo} rev-parse -q --verify "{pr.base.sha}^{{tree}}" > /dev/null 2>&1; then
    rm -rf /home/{pr.repo}
    git clone --quiet https://github.com/{pr.org}/{pr.repo}.git /home/{pr.repo}
fi
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

git checkout --detach {pr.base.sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"
bash /home/check_git_changes.sh

{toolchain}

python -m pip install --no-cache-dir -e ./aim/web/ui || true
python -m pip install --no-cache-dir --no-build-isolation -e . || true

python -c "import aimrocks, aim; print('aim', aim.__version__.__version__)"
pytest tests --collect-only -q -p no:cacheprovider > /dev/null

cat > /home/{pr.repo}/test_commands.sh <<'EOF'
#!/bin/bash
cd /home/{pr.repo}
rc=0
for test_file in $(find tests -name 'test_*.py' | sort); do
    pytest "$test_file" --no-header -rA --tb=no -p no:cacheprovider -v || rc=$?
done
exit $rc
EOF
chmod +x /home/{pr.repo}/test_commands.sh

""".format(pr=self.pr, toolchain=toolchain),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
bash /home/{pr.repo}/test_commands.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/{pr.repo}/test_commands.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/{pr.repo}/test_commands.sh

""".format(pr=self.pr),
            ),
        ]

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


@Instance.register("aimhubio", "aim_2922_to_2162")
class AIM_2922_TO_2162(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return AimImageDefault(self.pr, self._config)

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
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        re_result = re.compile(
            r"^(tests/\S+?::\S+?)\s+(PASSED|FAILED|SKIPPED|XPASS|XFAIL|ERROR)\b"
        )
        re_summary = re.compile(
            r"^(PASSED|FAILED|ERROR|XPASS|XFAIL)\s+(tests/\S+?::\S+?)(?:\s+-\s.*)?$"
        )

        for line in clean_log.split("\n"):
            line = line.strip()

            result_match = re_result.match(line)
            if result_match:
                name, status = result_match.group(1), result_match.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            summary_match = re_summary.match(line)
            if summary_match:
                status, name = summary_match.group(1), summary_match.group(2)
                if status in ("PASSED", "XPASS"):
                    passed_tests.add(name)
                elif status in ("FAILED", "ERROR"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
