import importlib
import re
import shlex
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:20"

_STAGE_JSON = "/home/stage.json"
_STAGE_TESTS = "/home/stage.tests"
_STAGE_EMPTY = "/home/stage.empty"
_PASS_A_JSON = "/home/test-pass-a.json"
_PASS_A_TESTS = "/home/test-pass-a.tests"
_PASS_A_EMPTY = "/home/test-pass-a.empty"
_PASS_B_JSON = "/home/test-pass-b.json"
_PASS_B_TESTS = "/home/test-pass-b.tests"
_PASS_B_EMPTY = "/home/test-pass-b.empty"

_VITEST_FLAGS = "--no-file-parallelism --testTimeout=60000 --hookTimeout=60000"


def _strip_diff_prefix(path: str) -> str:
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _patch_paths(patch: str) -> list[str]:
    paths: set[str] = set()
    for line in patch.split("\n"):
        if not line.startswith("diff --git"):
            continue
        parts = line.split()
        for raw in parts[2:4]:
            cleaned = _strip_diff_prefix(raw)
            if cleaned and cleaned != "/dev/null":
                paths.add(cleaned)
    return sorted(paths)


_EMIT_PY = r'''
import json
import sys

json_path, out_path, root, empty_path = sys.argv[1:5]

try:
    with open(json_path) as fh:
        data = json.load(fh)
except Exception:
    data = {}

prefix = root.rstrip("/") + "/"
suites = data.get("testResults") or []
lines = []
failures = []
empty = []

for suite in suites:
    name = (suite.get("name") or "").replace("\\", "/")
    if name.startswith(prefix):
        name = name[len(prefix):]
    while name.startswith("./"):
        name = name[2:]
    cases = suite.get("assertionResults") or []
    if not cases:
        if name:
            empty.append(name)
        continue
    for case in cases:
        title = (case.get("title") or "").strip()
        if not title:
            continue
        parts = [name]
        for anc in case.get("ancestorTitles") or []:
            anc = (anc or "").strip()
            if anc:
                parts.append(anc)
        parts.append(title)
        ident = " > ".join(parts).replace("\r", " ").replace("\n", " ")
        status = (case.get("status") or "").strip().lower()
        if status in ("failed", "error"):
            label = "FAILED"
            for msg in (case.get("failureMessages") or [])[:1]:
                head = str(msg).replace("\r", " ").split("\n")[0][:300]
                failures.append("FAILURE %s :: %s" % (ident, head))
        elif status in ("skipped", "pending", "todo", "disabled"):
            label = "SKIPPED"
        else:
            label = "PASSED"
        lines.append("TESTCASE %s %s" % (label, ident))

with open(out_path, "w") as fh:
    fh.write("".join(line + "\n" for line in lines))

with open(empty_path, "w") as fh:
    fh.write("".join(name + "\n" for name in sorted(set(empty))))

sys.stdout.write("STAGE_SUMMARY suites=%d testcases=%d nocases=%d\n" % (
    len(suites), len(lines), len(set(empty))))
for name in sorted(set(empty)):
    sys.stdout.write("NOCASES %s\n" % name)
for line in failures[:200]:
    sys.stdout.write(line + "\n")
'''

_MERGE_PY = r'''
import os
import sys


def load(path):
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        return [line.rstrip("\n") for line in fh if line.strip()]


def ident(line):
    parts = line.split(None, 2)
    return parts[2] if len(parts) > 2 else None


primary = load(sys.argv[1])
seen = {ident(line) for line in primary}
merged = list(primary)
for line in load(sys.argv[2]):
    key = ident(line)
    if key not in seen:
        seen.add(key)
        merged.append(line)
sys.stdout.write("".join(line + "\n" for line in merged))
'''

_HARDEN_BLOCK = """WORKDIR /home/{repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

"""

_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export npm_config_fund=false
export npm_config_audit=false

cd /home/{repo}
git reset --hard
git checkout {sha}
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

test -x node_modules/.bin/vitest

git checkout -- .
git clean -fd
bash /home/check_git_changes.sh
"""

_SCRIPT_PREAMBLE = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"

cd /home/{repo}
"""


def _vitest_block(json_path: str) -> str:
    return (
        f"rm -f {json_path}\n"
        f"if npx --no-install vitest run --reporter=json "
        f"--outputFile={json_path} {_VITEST_FLAGS}; then :; fi\n"
        f"test -s {json_path}\n"
    )


def _emit_block(repo: str, json_path: str, out_path: str, empty_path: str) -> str:
    args = " ".join([json_path, out_path, f"/home/{repo}", empty_path])
    return (
        f"python3 - {args} <<'EMIT_TESTCASES'\n"
        + _EMIT_PY.strip()
        + "\nEMIT_TESTCASES\n"
    )


def _graded_body(repo: str) -> str:
    return (
        _vitest_block(_STAGE_JSON)
        + _emit_block(repo, _STAGE_JSON, _STAGE_TESTS, _STAGE_EMPTY)
        + f"cat {_STAGE_TESTS}\n"
    )


def _test_stage_body(pr: PullRequest) -> str:
    repo = pr.repo
    body = (
        "git apply --whitespace=nowarn /home/test.patch\n\n"
        + _vitest_block(_PASS_A_JSON)
        + _emit_block(repo, _PASS_A_JSON, _PASS_A_TESTS, _PASS_A_EMPTY)
    )

    paths = _patch_paths(pr.test_patch)
    if paths:
        quoted = " ".join(shlex.quote(p) for p in paths)
        body += (
            "\nNEEDS_RECOVERY=0\n"
            f"while read -r nocase; do\n"
            'if [ -n "$nocase" ] && git cat-file -e "HEAD:$nocase" 2>/dev/null; then\n'
            "NEEDS_RECOVERY=1\n"
            "fi\n"
            f"done < {_PASS_A_EMPTY}\n"
            'if [ "$NEEDS_RECOVERY" = "1" ]; then\n'
            f"for path in {quoted}; do\n"
            'if git cat-file -e "HEAD:$path" 2>/dev/null; then\n'
            'git checkout HEAD -- "$path"\n'
            "else\n"
            'rm -f "$path"\n'
            "fi\n"
            "done\n"
            + _vitest_block(_PASS_B_JSON)
            + _emit_block(repo, _PASS_B_JSON, _PASS_B_TESTS, _PASS_B_EMPTY)
            + "fi\n"
        )

    body += (
        f"\npython3 - {_PASS_A_TESTS} {_PASS_B_TESTS} <<'MERGE_TESTCASES'\n"
        + _MERGE_PY.strip()
        + "\nMERGE_TESTCASES\n"
    )
    return body


class OhMyClaudecodeImageBase1075To786(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return "base-1075-786"

    def workdir(self) -> str:
        return "base-1075-786"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

{self.clear_env}

RUN git -C /home clone "${{REPO_URL}}" {self.pr.repo}

CMD ["/bin/bash"]
"""


class OhMyClaudecodeImageDefault1075To786(Image):
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
        return OhMyClaudecodeImageBase1075To786(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        preamble = _SCRIPT_PREAMBLE.format(repo=self.pr.repo)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            File(".", "run.sh", preamble + _graded_body(self.pr.repo)),
            File(".", "test-run.sh", preamble + _test_stage_body(self.pr)),
            File(
                ".",
                "fix-run.sh",
                preamble
                + "git apply --whitespace=nowarn /home/test.patch\n"
                + "git apply --whitespace=nowarn /home/fix.patch\n"
                + _graded_body(self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("The dependency of the default image must be an image.")

        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        harden_commands = _HARDEN_BLOCK.format(
            repo=self.pr.repo, sha=self.pr.base.sha
        )

        sections = [f"FROM {name}:{tag}"]
        for part in (
            self.global_env,
            harden_commands,
            copy_commands,
            "RUN bash /home/prepare.sh",
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def _disjoint(passed: set[str], failed: set[str], skipped: set[str]) -> TestResult:
    passed = passed - failed
    skipped = skipped - failed - passed
    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("Yeachan-Heo", "oh_my_claudecode_1075_to_786")
class OH_MY_CLAUDECODE_1075_TO_786(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeImageDefault1075To786(self.pr, self._config)

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
        clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in clean_log.split("\n"):
            match = _TESTCASE_RE.match(line)
            if not match:
                continue
            status, name = match.group(1), match.group(2)
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "SKIPPED":
                skipped_tests.add(name)
            else:
                passed_tests.add(name)

        return _disjoint(passed_tests, failed_tests, skipped_tests)


_BUNDLE_NUMBER_INTERVALS = [
    "785-786",
    "830-832",
    "892-896",
    "899-902",
    "921-922",
    "930-955",
    "937-958",
    "1021-1032",
    "1055-1073",
    "1062-1075",
]

for _ni in _BUNDLE_NUMBER_INTERVALS:
    Instance.register("Yeachan-Heo", _ni)(OH_MY_CLAUDECODE_1075_TO_786)


_ERA_NUMBERS = frozenset(
    {786, 832, 896, 902, 922, 955, 958, 1032, 1073, 1075}
)


def _select_config(number: int):
    if number in _ERA_NUMBERS:
        return OH_MY_CLAUDECODE_1075_TO_786
    module = importlib.import_module(
        "multi_swe_bench.harness.repos.typescript.Yeachan_Heo.oh_my_claudecode"
    )
    return module.OhMyClaudecode


@Instance.register("Yeachan-Heo", "oh-my-claudecode")
class OH_MY_CLAUDECODE(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        self._delegate = _select_config(pr.number)(pr, config, *args, **kwargs)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def delegate(self) -> Instance:
        return self._delegate

    def dependency(self) -> Optional[Image]:
        return self._delegate.dependency()

    def run(self, run_cmd: str = "") -> str:
        return self._delegate.run(run_cmd)

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return self._delegate.test_patch_run(test_patch_run_cmd)

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return self._delegate.fix_patch_run(fix_patch_run_cmd)

    def parse_log(self, test_log: str) -> TestResult:
        return self._delegate.parse_log(test_log)
