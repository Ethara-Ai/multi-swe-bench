import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_TEST_PACKAGES = """MODULE=$(go list -m)
PKGS=$(go list ./... | grep -vE "^${MODULE}/(modelmigration(/.*)?|tests|tests/integration|tests/integration/migration-test)$")"""

_TEST_CMD = "go test -json -count=1 -timeout 90m -skip '^(TestDebounce|TestPullRequest_AddToTaskQueue)$' $PKGS || status=$?"

_INTEGRATION_FILE = re.compile(r"^tests/integration/[^/]+_test\.go$")
_ADDED_TEST = re.compile(r"^\+func (Test\w+)\(")
_HUNK_TEST = re.compile(r"^@@ [^@]* @@ func (Test\w+)\(")


def _integration_tests(test_patch: str) -> list[str]:
    names = set()
    current = None
    for line in test_patch.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/\S+ b/(\S+)", line)
            current = m.group(1) if m else None
            continue
        if not current or not _INTEGRATION_FILE.match(current):
            continue
        m = _ADDED_TEST.match(line) or _HUNK_TEST.match(line)
        if m and m.group(1) != "TestMain":
            names.add(m.group(1))
    return sorted(names)


def _integration_cmd(tests: list[str]) -> str:
    if not tests:
        return ""
    # Integration tests exec the gitea binary from the source root (setting.AppPath).
    return (
        "(go build -o gitea . && GITEA_TEST_DATABASE=sqlite "
        "go test -json -count=1 -timeout 40m "
        f"-run '^({'|'.join(tests)})$' ./tests/integration) || status=$?"
    )


def _integration_warmup(tests: list[str]) -> str:
    if not tests:
        return ""
    return "go build -o /dev/null .\ngo test -count=1 -exec /bin/true ./tests/integration"

_PR_INTERVAL = "30205_to_40000"
_TEST_USER = "giteatest"
_BASE_TAG = f"base-{_PR_INTERVAL}"
_GO_IMAGE = "golang:1.27"
_TEST_TIMEOUT = "40m"
_INTEGRATION_DIR = "tests/integration"
_EXCLUDED_DIR_PREFIXES = ("vendor/", "modelmigration/", "tests/", "build/", "tools/")

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)


def _patch_paths(*patches: str) -> list[str]:
    paths: set[str] = set()
    for patch in patches:
        for old, new in _DIFF_HEADER.findall(patch):
            paths.add(old)
            paths.add(new)
    return sorted(p for p in paths if p != "/dev/null")


def _unit_packages(*patches: str) -> list[str]:
    dirs: set[str] = set()
    for path in _patch_paths(*patches):
        if not path.endswith(".go"):
            continue
        if "/testdata/" in f"/{path}":
            continue
        if path.startswith(_EXCLUDED_DIR_PREFIXES):
            continue
        dirs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(dirs)


def _integration_test_files(*patches: str) -> list[str]:
    files: set[str] = set()
    for path in _patch_paths(*patches):
        if not path.endswith("_test.go"):
            continue
        if not path.startswith(f"{_INTEGRATION_DIR}/"):
            continue
        if path.startswith(f"{_INTEGRATION_DIR}/migration-test/"):
            continue
        files.add(path)
    return sorted(files)


def _test_command(test_patch: str, fix_patch: str) -> str:
    packages = " ".join(f"'{d}'" for d in _unit_packages(test_patch, fix_patch))
    integration = " ".join(
        f"'{f}'" for f in _integration_test_files(test_patch, fix_patch)
    )

    lines = ['PKGS=""']
    if packages:
        lines += [
            f"for d in {packages}; do",
            '  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then',
            '    PKGS="$PKGS ./$d"',
            "  fi",
            "done",
        ]

    lines.append('INTEG_RUN=""')
    if integration:
        lines += [
            'INTEG_NAMES=""',
            f"for f in {integration}; do",
            '  if [ -f "$f" ]; then',
            "    INTEG_NAMES=\"$INTEG_NAMES $(sed -n 's/^func \\(Test[A-Za-z0-9_]*\\)(.*/\\1/p' \"$f\")\"",
            "  fi",
            "done",
            'if [ -n "$(printf \'%s\' $INTEG_NAMES)" ]; then',
            "  INTEG_SEED=$(awk 'FNR==1{n=\"\"}"
            " /^func Test/{n=$0; sub(/^func /,\"\",n); sub(/\\(.*/,\"\",n)}"
            " /tests\\.PrepareTestEnv/{if(n!=\"\"){print n; exit}}'"
            f" $(ls {_INTEGRATION_DIR}/*_test.go 2>/dev/null | sort))",
            '  INTEG_NAMES="$INTEG_NAMES $INTEG_SEED"',
            "fi",
            "INTEG_RUN=$(printf '%s\\n' $INTEG_NAMES | sort -u | paste -sd'|' -)",
        ]

    lines += [
        "status=0",
        'if [ -n "$PKGS" ]; then',
        f"  go test -json -count=1 -timeout {_TEST_TIMEOUT} $PKGS || status=$?",
        "fi",
        'if [ -n "$INTEG_RUN" ]; then',
        "  go build -o gitea .",
        f'  go test -json -count=1 -timeout {_TEST_TIMEOUT} -run "^($INTEG_RUN)$" ./{_INTEGRATION_DIR} || status=$?',
        "fi",
        'exit "$status"',
    ]
    return "\n".join(lines)


class GiteaImageBase_30205_TO_40000(Image):
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
        return _GO_IMAGE
    def dependency(self) -> str | Image:
        return "golang:1.27.1-bookworm"

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{repo}'
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

ENV CI=true \\
    CGO_ENABLED=0 \\
    GOTOOLCHAIN= \\
    GOFLAGS=-buildvcs=false \\
    SNAP=1 \\
    SNAP_NAME=gitea

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
CMD ["/bin/bash"]
"""


class GiteaImageDefault_30205_TO_40000(Image):
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
        return GiteaImageBase_30205_TO_40000(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        integration_tests = _integration_tests(self.pr.test_patch)
        integration = _integration_cmd(integration_tests)
        warmup = _integration_warmup(integration_tests)

        test_command = _test_command(self.pr.test_patch, self.pr.fix_patch)
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefailo pipefail
export CI=true

cd /home/{repo}
git reset --hard
git clean -fdx
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch --no-tags origin {pr.base.sha}
git checkout --detach --detach {sha}
bash /home/check_git_changes.sh

GO_VERSION=$(awk '/^toolchain /{{print $2; exit}}' go.mod)
if [ -z "$GO_VERSION" ]; then
  GO_VERSION=go$(awk '/^go /{{print $2; exit}}' go.mod)
  case "$GO_VERSION" in
    go*.*.*) ;;
    *) GO_VERSION="$GO_VERSION.0" ;;
  esac
fi
go env -w GOTOOLCHAIN="$GO_VERSION"
go version

go mod download

{packages}
go test -count=1 -exec /bin/true $PKGS
{warmup}
echo DEPS_OK

""".format(
                    repo=self.pr.repo,
                    sha=self.pr.base.sha,
                    packages=_TEST_PACKAGES,
                    warmup=warmup,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{pr.repo}
{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=trueo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{packages}
status=0
{cmd}
{integration}
exit $status

""".format(
                    repo=self.pr.repo,
                    packages=_TEST_PACKAGES,
                    cmd=_TEST_CMD,
                    integration=integration,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=trueo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{packages}
status=0
{cmd}
{integration}
exit $status

""".format(
                    repo=self.pr.repo,
                    packages=_TEST_PACKAGES,
                    cmd=_TEST_CMD,
                    integration=integration,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

{Image._HARDENING_BLOCK}

{self.clear_env}
"""


@Instance.register("go-gitea", "gitea")
class GITEA_30205_TO_40000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return GiteaImageDefault_30205_TO_40000(self.pr, self._config)

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
        packages_with_tests: set[str] = set()
        broken_packages: set[str] = set()

        for line in log.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            test = event.get("Test")
            action = event.get("Action")
            if not test or action not in ("pass", "fail", "skip"):
                continue

            name = f"{event.get('Package', '')}::{test}"
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
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
