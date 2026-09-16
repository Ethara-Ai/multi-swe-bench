from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_YARN_BERRY_FIRST_PR = 3000

_SHARED_BASE_IMAGE = "node:16-bullseye"
_BASE_IMAGE_TAG = "base"

_LEGACY_NODE_VERSION = "14.21.3"
_LEGACY_YARN_VERSION = "1.22.19"
_LEGACY_NODE_PREFIX = "/opt/node14"

_TARGET_PACKAGES: dict[int, tuple[str, ...]] = {
    1854: ("packages/cli",),
    3536: ("packages/cli",),
    3598: ("packages/cli",),
    3616: ("packages/cli",),
    3772: ("packages/router", "packages/testing"),
}

_DEFAULT_TARGET_PACKAGES: tuple[str, ...] = ("packages/cli",)

_JSON_BEGIN = "===== MSB-JEST-BEGIN"
_JSON_END = "===== MSB-JEST-END"

_REPO_ROOT = "/home/graphql"

_TARGET_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)

_TEST_EXT = (".ts", ".tsx", ".js", ".jsx")


def _patch_paths(patch: Optional[str]) -> list[str]:
    out = []
    for raw in _TARGET_RE.findall(patch or ""):
        path = raw.strip()
        if not path or path == "/dev/null":
            continue
        out.append(path)
    return out


def _test_files(pr: PullRequest) -> list[str]:
    out = []
    for path in _patch_paths(pr.test_patch):
        parts = path.split("/")
        if "fixtures" in parts or "__fixtures__" in parts:
            continue
        base = parts[-1]
        if base.endswith(".d.ts"):
            continue
        if not base.endswith(_TEST_EXT):
            continue
        if ".test." not in base and ".spec." not in base:
            continue
        out.append(path)
    return sorted(set(out))


def _target_packages(number: int) -> tuple[str, ...]:
    return _TARGET_PACKAGES.get(number, _DEFAULT_TARGET_PACKAGES)


def _stage_env(number: int) -> str:
    lines = [
        "export CI=true",
        'export NODE_OPTIONS="--max-old-space-size=4096"',
    ]
    if _uses_legacy_node(number):
        lines.append(f'export PATH="{_LEGACY_NODE_PREFIX}/bin:$PATH"')
    if _uses_yarn_berry(number):
        lines.append("export YARN_ENABLE_IMMUTABLE_INSTALLS=false")
        lines.append("export YARN_NODE_LINKER=node-modules")
        lines.append("export YARN_HTTP_TIMEOUT=600000")
    return "\n".join(lines)


def _install_command(number: int) -> str:
    if _uses_yarn_berry(number):
        return "yarn install"
    return "yarn install --frozen-lockfile"

def _test_body(pr: PullRequest) -> str:
    packages = " ".join(_target_packages(pr.number))
    return f"""
PACKAGES="{packages}"

for pkg in $PACKAGES; do
    cd /home/{pr.repo}/$pkg
    set +e
    yarn build:js
    BUILD_RC=$?
    set -e
    if [ "$BUILD_RC" -ne 0 ]; then
        echo "NOTE: yarn build:js exited $BUILD_RC in $pkg; the suite runs against the previously built output"
    fi
done

for pkg in $PACKAGES; do
    cd /home/{pr.repo}/$pkg
    REPORT="/tmp/jest-$(echo $pkg | tr / _).json"
    rm -f "$REPORT"
    set +e
    yarn jest src --json --outputFile="$REPORT" --colors=false --maxWorkers=2
    JEST_RC=$?
    set -e
    if [ ! -s "$REPORT" ]; then
        echo "Error: jest wrote no report for $pkg (exit $JEST_RC)" >&2
        exit 1
    fi
    echo "{_JSON_BEGIN}"
    cat "$REPORT"
    echo
    echo "{_JSON_END}"
done
"""


def parse_jest_json_log(log: str, repo: str) -> TestResult:
    clean = _ANSI_ESCAPE.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    occurrences: dict[str, int] = {}

    prefix = f"/home/{repo}/"

    for block in _JSON_BLOCK.findall(clean):
        try:
            report = json.loads(block)
        except ValueError:
            continue

        for suite in report.get("testResults") or []:
            path = (suite.get("name") or "").replace("\\", "/")
            if path.startswith(prefix):
                path = path[len(prefix) :]

            assertions = suite.get("assertionResults") or []
            if not assertions:
                if suite.get("status") == "failed":
                    failed_tests.add(path)
                continue

            for assertion in assertions:
                titles = [t for t in (assertion.get("ancestorTitles") or []) if t]
                name = " > ".join([path] + titles + [assertion.get("title") or ""])

                occurrences[name] = occurrences.get(name, 0) + 1
                if occurrences[name] > 1:
                    name = f"{name} #{occurrences[name]}"

                status = assertion.get("status")
                if status == "passed":
                    passed_tests.add(name)
                elif status == "failed":
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



class RedwoodjsGraphqlImageBase(Image):

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
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()

        sections = [f"FROM {base_image}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(_APT_PACKAGES)
        sections.append(_LEGACY_NODE_SETUP)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


_PREPARE_TEMPLATE = """#!/bin/bash
set -e
{env}

git config --global --add safe.directory /home/{repo}
git config --global url."https://github.com/".insteadOf "git://github.com/"

git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 300

for attempt in 1 2 3 4 5; do
    rm -rf /home/{repo}
    if git clone "{repo_url}" /home/{repo}; then
        break
    fi
    echo "clone attempt $attempt failed; retrying in 20s" >&2
    sleep 20
done
if [ ! -d /home/{repo}/.git ]; then
    echo "FATAL: git clone failed after 5 attempts" >&2
    exit 1
fi
cd /home/{repo}

git checkout --detach {sha}
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

test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi

git reset --hard
bash /home/check_git_changes.sh

for attempt in 1 2 3; do
    if {install}; then
        break
    fi
    echo "install attempt $attempt failed; retrying in 20s" >&2
    sleep 20
done
yarn build || true
"""


class RedwoodjsGraphqlImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:  # type: ignore
        return RedwoodjsGraphqlImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        org = self.pr.org
        sha = self.pr.base.sha

        test_paths = _sh_list(_test_files(self.pr))

        def _fill(text: str) -> str:
            return (
                text.replace("[[TEST_PATHS]]", test_paths)
                .replace("[[BEGIN]]", _JSON_BEGIN)
                .replace("[[END]]", _JSON_END)
                .replace("[[ROOT]]", _REPO_ROOT)
                .replace("[[ORG]]", org)
                .replace("[[REPO]]", repo)
                .replace("[[SHA]]", sha)
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _fill(_CHECK_GIT_CHANGES_SH)),
            File(".", "compile.sh", _fill(_COMPILE_SH)),
            File(".", "run_tests.sh", _fill(_RUN_TESTS_SH)),
            File(".", "prepare.sh", _fill(_PREPARE_SH)),
            File(".", "run.sh", _fill(_RUN_SH)),
            File(".", "test-run.sh", _fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", _fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()  # type: ignore
        tag = image.image_tag()  # type: ignore
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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


@Instance.register("redwoodjs", "graphql")
class RedwoodjsGraphql(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:  # type: ignore
        return RedwoodjsGraphqlImageDefault(self.pr, self._config)

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
        return parse_jest_json_log(test_log, self.pr.repo)




