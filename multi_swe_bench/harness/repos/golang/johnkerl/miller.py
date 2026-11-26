from __future__ import annotations

import json
import posixpath
import re

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_GO_IMAGE = "golang:1.26-bookworm"
_BASE_TAG = "base"
_GO_TEST_TIMEOUT = "30m"
_GIT_APPLY = (
    "git apply --whitespace=nowarn"
    " --exclude='docs/src/*.md' --exclude='docs/src/manpage.txt' --exclude='man/*'"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)
_CASE_LINE_RE = re.compile(r"^(pass|fail|skip) (test/cases/\S+)\s*$")
_MODULE_RE = re.compile(r"^github\.com/johnkerl/miller(?:/v\d+)?(?:/(.*))?$")

_CHECK_GIT_CHANGES_SH = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def _changed_paths(pr: PullRequest) -> list[str]:
    return _DIFF_HEADER_RE.findall(pr.test_patch or "")


def _case_groups(pr: PullRequest) -> list[str]:
    groups: set[str] = set()
    for path in _changed_paths(pr):
        parts = path.split("/")
        if len(parts) >= 4 and parts[0] == "test" and parts[1] == "cases":
            groups.add("/".join(parts[:3]))
    return sorted(groups)


def _go_packages(pr: PullRequest) -> list[str]:
    packages: set[str] = set()
    for path in _changed_paths(pr):
        if path.endswith("_test.go"):
            directory = posixpath.dirname(path)
            packages.add(f"./{directory}" if directory else ".")
    return sorted(packages)


def _existing(var: str, paths: list[str], probe: str) -> str:
    return f"""\
{var}=()
for p in {" ".join(paths)}; do
  if {probe}; then
    {var}+=("$p")
  else
    echo "note: $p is not present in this tree, skipped" >&2
  fi
done"""


def _select(pr: PullRequest) -> str:
    groups = _case_groups(pr)
    packages = _go_packages(pr)
    if not groups and not packages:
        raise ValueError(
            f"{pr.org}/{pr.repo} PR #{pr.number}: test patch touches no regression case or _test.go file"
        )
    lines = []
    lines.append(_existing("cases", groups, '[ -d "$p" ]') if groups else "cases=()")
    lines.append(
        _existing("pkgs", packages, 'compgen -G "$p/*.go" > /dev/null')
        if packages
        else "pkgs=()"
    )
    return "\n".join(lines)


def _prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -e

cd /home/{pr.repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
bash /home/check_git_changes.sh

for attempt in 1 2 3; do
  go mod download && break || true
  [ "$attempt" -lt 3 ] || break
  echo "prepare.sh: go mod download attempt $attempt failed; retrying in 15s"
  sleep 15
done

go build -o /tmp/mlr/mlr ./cmd/mlr
{_select(pr)}
if [ "${{#pkgs[@]}}" -gt 0 ]; then
  go test -count=1 -vet=off -run '^$' -exec /bin/true "${{pkgs[@]}}"
fi
/tmp/mlr/mlr --version
echo DEPS_OK
"""


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail
export CI=true
export MLR_NO_COLOR=true

cd /home/{pr.repo}
{apply_line}
{_select(pr)}

go build -o /tmp/mlr/mlr ./cmd/mlr
export PATH="/tmp/mlr:$PATH"

rc=0
if [ "${{#cases[@]}}" -gt 0 ]; then
  mlr regtest -v "${{cases[@]}}" || rc=$?
fi
if [ "${{#pkgs[@]}}" -gt 0 ]; then
  go test -json -count=1 -vet=off -timeout={_GO_TEST_TIMEOUT} "${{pkgs[@]}}" || rc=$?
fi
exit "$rc"
"""


class MillerImageBase(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        dockerfile = f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {_GO_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

ENV LC_ALL=C.UTF-8 \\
    GOTOOLCHAIN=local \\
    CGO_ENABLED=0

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates tzdata \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""
        return _BLANK_RUN_RE.sub("\n\n", dockerfile)


class MillerImageDefault(Image):
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
        return MillerImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _prepare_sh(self.pr)),
            File(".", "run.sh", _run_sh(self.pr, "")),
            File(
                ".",
                "test-run.sh",
                _run_sh(self.pr, f"{_GIT_APPLY} /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_sh(self.pr, f"{_GIT_APPLY} /home/test.patch /home/fix.patch"),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dockerfile = f"""FROM {image.image_full_name()}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK.rstrip(chr(10))}

{self.clear_env}
"""
        return _BLANK_RUN_RE.sub("\n\n", dockerfile)


def _test_label(package: str, test: str) -> str | None:
    match = _MODULE_RE.match(package)
    if match is None:
        return None
    return f"{match.group(1) or '.'}::{test}"


@Instance.register("johnkerl", "miller")
class Miller(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return MillerImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        buckets = {"pass": passed, "fail": failed, "skip": skipped}

        for raw in _ANSI_RE.sub("", test_log).splitlines():
            line = raw.strip()
            case = _CASE_LINE_RE.match(line)
            if case:
                buckets[case.group(1)].add(case.group(2))
                continue
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            action = event.get("Action")
            if action not in buckets:
                continue
            test = event.get("Test")
            if test:
                label = _test_label(event.get("Package") or "", test)
            elif action == "fail" and event.get("FailedBuild"):
                label = _test_label(event.get("Package") or "", "[build failed]")
            else:
                label = None
            if label:
                buckets[action].add(label)

        passed -= failed
        skipped -= failed
        passed -= skipped

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
