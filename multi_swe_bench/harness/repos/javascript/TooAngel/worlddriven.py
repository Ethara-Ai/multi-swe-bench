import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

TEST_CMD = (
    "python -m pytest tests/ -rA -p no:cacheprovider "
    "--continue-on-collection-errors --tb=no"
)

MONGO_BOOT = """mkdir -p /data/db
if ! mongod --fork --logpath /tmp/mongod.log --dbpath /data/db --bind_ip 127.0.0.1; then
    echo "FATAL: mongod failed to fork -- the graded run cannot proceed." >&2
    cat /tmp/mongod.log >&2 || true
    exit 1
fi
for _ in $(seq 1 30); do
    if (echo > /dev/tcp/127.0.0.1/27017) 2>/dev/null; then break; fi
    sleep 1
done
if ! (echo > /dev/tcp/127.0.0.1/27017) 2>/dev/null; then
    echo "FATAL: mongod never became ready on 127.0.0.1:27017 after 30s." >&2
    cat /tmp/mongod.log >&2 || true
    exit 1
fi"""

PIP_PINS = (
    '"Jinja2==2.11.3" "MarkupSafe==1.1.1" "Werkzeug==1.0.1" '
    '"itsdangerous==1.1.0" "click==7.1.2" "tzlocal==2.1" "pymongo==3.11.4"'
)

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_APPLY_PATCH_SH = r"""#!/bin/bash

patch_file="$1"

if [ ! -s "$patch_file" ]; then
    echo "apply_patch: $patch_file is empty or missing; nothing to apply"
    exit 0
fi

if git apply --check --whitespace=nowarn "$patch_file" 2>/dev/null; then
    if git apply --whitespace=nowarn "$patch_file" 2>/dev/null; then
        echo "apply_patch: $patch_file -> applied whole (fast path)"
        exit 0
    fi
fi

split_dir="$(mktemp -d)"
csplit -z -s -f "$split_dir/sec" -b '%05d.patch' "$patch_file" '/^diff --git /' '{*}' \
    2>/dev/null || cp "$patch_file" "$split_dir/sec00000.patch"

section_paths() {
    sed -n -e 's|^--- a/||p' -e 's|^+++ b/||p' "$1" \
        | grep -v '^/dev/null$' | sort -u
}

revert_section() {
    local p
    for p in $(section_paths "$1"); do
        if git cat-file -e "HEAD:$p" 2>/dev/null; then
            git checkout HEAD -- "$p" 2>/dev/null || true
        else
            git rm -f -q --cached "$p" 2>/dev/null || true
            rm -f "$p" 2>/dev/null || true
        fi
    done
}

apply_one() {
    local sec="$1"
    git apply --whitespace=nowarn "$sec" 2>/dev/null && return 0
    if git apply --3way --whitespace=nowarn "$sec" 2>/dev/null; then return 0; fi
    revert_section "$sec"
    git apply --whitespace=nowarn -C1 --recount "$sec" 2>/dev/null && return 0
    if patch -p1 --forward --batch --fuzz=3 --dry-run -i "$sec" >/dev/null 2>&1; then
        patch -p1 --forward --batch --fuzz=3 --no-backup-if-mismatch \
            -r /dev/null -i "$sec" >/dev/null 2>&1 && return 0
    fi
    return 1
}

applied=0
rejected=0
rejected_files=""

for sec in "$split_dir"/sec*.patch; do
    [ -s "$sec" ] || continue
    target="$(sed -n 's|^diff --git a/\(.*\) b/.*|\1|p' "$sec" | head -1)"
    [ -n "$target" ] || target="(preamble)"
    if apply_one "$sec"; then
        applied=$((applied + 1))
    else
        rejected=$((rejected + 1))
        rejected_files="$rejected_files $target"
    fi
done

rm -rf "$split_dir"

echo "apply_patch: $patch_file -> $applied file(s) applied, $rejected rejected"
if [ "$rejected" -gt 0 ]; then
    echo "apply_patch: rejected:"
    for f in $rejected_files; do echo "apply_patch:   $f"; done
    echo "$rejected $patch_file" >> /tmp/apply_patch_rejects
fi

exit 0
"""

_REJECT_BANNER = """if [ -s /tmp/apply_patch_rejects ]; then
    echo "=================================================================="
    echo "WARNING: a patch did NOT fully apply -- results below are suspect:"
    cat /tmp/apply_patch_rejects
    echo "=================================================================="
fi"""


class WorlddrivenImageBase(Image):
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
        return "python:3.7-slim"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

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

        return f"""FROM {image_name}

{self.global_env}

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git patch gnupg procps build-essential \\
    && curl -fsSL https://pgp.mongodb.com/server-7.0.asc \\
       | gpg --dearmor -o /usr/share/keyrings/mongo.gpg \\
    && echo "deb [signed-by=/usr/share/keyrings/mongo.gpg] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" \\
       > /etc/apt/sources.list.d/mongo.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends mongodb-org-server \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class WorlddrivenImageDefault(Image):
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
        return WorlddrivenImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "apply_patch.sh", _APPLY_PATCH_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

pip install --no-cache-dir --upgrade "pip<24" || true
pip install --no-cache-dir -r requirements.txt || true
pip install --no-cache-dir {pip_pins} || true

python --version
python -m pytest --version
python -c "import mock, pymongo, flask, apscheduler; print('test deps OK')"
PYTHONPATH=/home/{pr.repo}/src python -c "import PullRequest, server; print('imports OK')"
""".format(pr=self.pr, pip_pins=PIP_PINS),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export DISABLE_WORKER=true
export PYTHONPATH=/home/{pr.repo}/src

{mongo_boot}

cd /home/{pr.repo}
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD, mongo_boot=MONGO_BOOT),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export DISABLE_WORKER=true
export PYTHONPATH=/home/{pr.repo}/src

{mongo_boot}

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet
bash /home/apply_patch.sh /home/test.patch
{banner}
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD, mongo_boot=MONGO_BOOT, banner=_REJECT_BANNER),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export DISABLE_WORKER=true
export PYTHONPATH=/home/{pr.repo}/src

{mongo_boot}

cd /home/{pr.repo}
rm -f /tmp/apply_patch_rejects
git reset --hard --quiet
bash /home/apply_patch.sh /home/test.patch
bash /home/apply_patch.sh /home/fix.patch
{banner}
{test_cmd}
""".format(pr=self.pr, test_cmd=TEST_CMD, mongo_boot=MONGO_BOOT, banner=_REJECT_BANNER),
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


@Instance.register("TooAngel", "worlddriven")
class Worlddriven(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return WorlddrivenImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        result_re = re.compile(
            r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)(?:\s+\[\s*\d+\s*\])?\s+(\S+)"
        )

        for raw in log.splitlines():
            m = result_re.match(raw.strip())
            if not m:
                continue
            status, name = m.group(1), m.group(2).strip().rstrip(":")
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
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
