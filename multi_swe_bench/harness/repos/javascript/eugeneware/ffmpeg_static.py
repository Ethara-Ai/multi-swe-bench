import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NPM_INSTALL = (
    "npm install --unsafe-perm --no-audit --no-fund --loglevel=error"
)

_TEST_BODY = """\
set +e

{npm_install} > /tmp/npm-install.out 2>&1
NPM_RC=$?

: > /tmp/tap.out
for _f in test/*.js; do
    echo "##### TAPFILE $_f" >> /tmp/tap.out
    node_modules/.bin/tape "$_f" >> /tmp/tap.out 2>&1
done
set -e

cat /tmp/tap.out

if [ "$NPM_RC" -ne 0 ]; then
    echo "NOTE: npm install exited $NPM_RC; tail of its output follows"
    tail -20 /tmp/npm-install.out
fi

grep -q "^TAP version" /tmp/tap.out
"""


class FfmpegStaticImageBase(Image):
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
        return "node:14-bullseye"

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

WORKDIR /home/

{code}

{self.clear_env}

"""


class FfmpegStaticImageDefault(Image):
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
        return FfmpegStaticImageBase(self.pr, self._config)

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

{npm_install} || true

""".format(pr=self.pr, npm_install=_NPM_INSTALL),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, npm_install=_NPM_INSTALL),
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


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_TAPFILE = re.compile(r"^#####\s+TAPFILE\s+(\S+)\s*$")
_TAP_VERSION = re.compile(r"^TAP version\s+\d+\s*$")
_PLAN = re.compile(r"^\d+\.\.\d+\s*$")
_ASSERT = re.compile(r"^(not )?ok\b\s*(\d+)?\s*(?:-\s+)?(.*)$")
_COMMENT = re.compile(r"^#\s*(.*?)\s*$")
_SUMMARY_BODY = re.compile(r"^(?:tests|pass|fail|ok|not ok|skip|todo)\b", re.I)
_DIRECTIVE = re.compile(r"#\s*(SKIP|TODO)\b", re.I)


def parse_tape_tap_log(log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    clean = ANSI_ESCAPE.sub("", log)

    state = {
        "file": None,
        "test": None,
        "failed": False,
        "asserts": 0,
        "skips": 0,
        "plan": False,
        "any_test": False,
    }

    def close_test(aborted: bool) -> None:
        name = state["test"]
        if name is None:
            return
        if aborted or state["failed"]:
            failed_tests.add(name)
        elif state["asserts"] and state["asserts"] == state["skips"]:
            skipped_tests.add(name)
        else:
            passed_tests.add(name)
        state["test"] = None
        state["failed"] = False
        state["asserts"] = 0
        state["skips"] = 0

    def end_file() -> None:
        close_test(aborted=not state["plan"])
        if state["file"] is not None and not state["any_test"]:
            failed_tests.add(f"{state['file']} > <no tests ran>")
        state["file"] = None
        state["plan"] = False
        state["any_test"] = False

    for raw in clean.splitlines():
        line = raw.rstrip()

        m = _TAPFILE.match(line)
        if m:
            end_file()
            state["file"] = m.group(1)
            continue

        if _TAP_VERSION.match(line):
            continue

        if _PLAN.match(line):
            state["plan"] = True
            close_test(aborted=False)
            continue

        m = _COMMENT.match(line)
        if m:
            body = m.group(1)
            if not body or _SUMMARY_BODY.match(body):
                continue
            close_test(aborted=False)
            prefix = f"{state['file']} > " if state["file"] else ""
            state["test"] = f"{prefix}{body}"
            state["any_test"] = True
            continue

        m = _ASSERT.match(line)
        if m and state["test"] is not None:
            desc = m.group(3) or ""
            state["asserts"] += 1
            if _DIRECTIVE.search(desc):
                state["skips"] += 1
            elif m.group(1):
                state["failed"] = True
            continue

    end_file()

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


@Instance.register("eugeneware", "ffmpeg-static")
class FfmpegStatic(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return FfmpegStaticImageDefault(self.pr, self._config)

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
        return parse_tape_tap_log(log)
