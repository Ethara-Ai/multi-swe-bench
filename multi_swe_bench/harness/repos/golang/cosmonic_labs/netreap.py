import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "golang:1.20-bullseye"

GO_TEST_CMD = "go test -json -count=1 -timeout 20m ./..."

_GTF_EMIT = (
    'echo "### GTF_BEGIN ###"\n'
    "grep -rIn --include='*_test.go' -E "
    "'^func (Test|Example|Benchmark|Fuzz)[A-Za-z0-9_]*[(]' . 2>/dev/null "
    "| sed -E 's|^[.]/||' "
    "| sed -E 's|^([^:]+):[0-9]+:func ([A-Za-z0-9_]+).*|### GTF \\1 \\2 ###|' "
    "|| true\n"
    'echo "### GTF_END ###"'
)

_GO_ENV = """export CI=true
export GOFLAGS=-buildvcs=false
export CGO_ENABLED=0
export GOPATH=/root/go
export GOCACHE=/root/.cache/go-build"""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_GTF_RE = re.compile(r"^### GTF (\S+) (\S+) ###$")
_BUILD_FAIL_RE = re.compile(r"^FAIL\s+(\S+)\s+\[(?:build|setup) failed\]")


class NetreapImageBase(Image):
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
        return BASE_IMAGE

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
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        infra = DockerfileEnhancer._infrastructure_block(self, image_name).rstrip("\n")

        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            infra,
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append("ENV CGO_ENABLED=0 \\\n    GOFLAGS=-buildvcs=false")
        sections.append(code)
        sections.append(f"WORKDIR /home/{self.pr.repo}")

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class NetreapImageDefault(Image):
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
        return NetreapImageBase(self.pr, self.config)

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

{env}

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

go mod download || true
go build ./... || true
go test -count=1 -timeout 20m ./... || true

if git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    go mod download || true
    go build ./... || true
    go test -count=1 -timeout 20m ./... || true
fi
git checkout -- . || true
git reset --hard
git clean -fd
bash /home/check_git_changes.sh

go version
""".format(pr=self.pr, env=_GO_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}
{gtf}
{test_cmd}
""".format(pr=self.pr, env=_GO_ENV, gtf=_GTF_EMIT, test_cmd=GO_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply test.patch failed" >&2
    exit 1
fi
{gtf}
{test_cmd}
""".format(pr=self.pr, env=_GO_ENV, gtf=_GTF_EMIT, test_cmd=GO_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

{env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply test.patch + fix.patch failed" >&2
    exit 1
fi
{gtf}
{test_cmd}
""".format(pr=self.pr, env=_GO_ENV, gtf=_GTF_EMIT, test_cmd=GO_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {sha}

RUN set -eux; \\
    git checkout --detach "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "{sha}")"; \\
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

{self.clear_env}

"""


@Instance.register("cosmonic-labs", "netreap")
class Netreap(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NetreapImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def _rel_pkg(self, pkg: str) -> str:
        if not pkg:
            return ""
        marker = f"/{self.pr.repo}/"
        idx = pkg.find(marker)
        if idx != -1:
            return pkg[idx + len(marker) :]
        if pkg.endswith(f"/{self.pr.repo}") or pkg == self.pr.repo:
            return ""
        return pkg

    def _test_id(
        self,
        reldir: str,
        test_name: str,
        file_map: dict[tuple[str, str], str],
    ) -> str:
        base_func = test_name.split("/", 1)[0]
        relfile = file_map.get((reldir, base_func))
        if relfile:
            return f"{relfile}::{test_name}"
        if reldir:
            return f"{reldir}::{test_name}"
        return test_name

    def parse_log(self, test_log: str) -> TestResult:
        test_log = _ANSI_RE.sub("", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        file_map: dict[tuple[str, str], str] = {}
        pkg_funcs: dict[str, set[str]] = {}
        for line in test_log.splitlines():
            m = _GTF_RE.match(line.strip())
            if m:
                relfile, func = m.group(1), m.group(2)
                reldir = relfile.rsplit("/", 1)[0] if "/" in relfile else ""
                file_map[(reldir, func)] = relfile
                pkg_funcs.setdefault(reldir, set()).add(func)

        tests_seen: dict[str, set[str]] = {}
        broken_pkgs: set[str] = set()

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = _BUILD_FAIL_RE.match(line)
            if m:
                broken_pkgs.add(self._rel_pkg(m.group(1)))
                continue

            if not line.startswith("{") or '"Action"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue

            action = event.get("Action")
            reldir = self._rel_pkg(event.get("Package", ""))
            test_name = event.get("Test")

            if not test_name:
                if action == "output":
                    m = _BUILD_FAIL_RE.match((event.get("Output") or "").strip())
                    if m:
                        broken_pkgs.add(self._rel_pkg(m.group(1)))
                elif action == "fail":
                    broken_pkgs.add(reldir)
                continue

            if action not in ("pass", "fail", "skip"):
                continue

            tests_seen.setdefault(reldir, set()).add(test_name)
            test_id = self._test_id(reldir, test_name, file_map)

            if action == "pass":
                passed_tests.add(test_id)
            elif action == "fail":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        for reldir in broken_pkgs:
            if tests_seen.get(reldir):
                continue
            for func in pkg_funcs.get(reldir, ()):
                failed_tests.add(self._test_id(reldir, func, file_map))

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
