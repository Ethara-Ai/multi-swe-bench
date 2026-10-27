from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_ORG = "go-task"
_REPO = "task"

_ERA_FILE = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"task_(\d+)_to_(\d+)", _ERA_FILE)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like task_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_ERA_KEY = f"{_ERA_HI}-{_ERA_LO}"
_BASE_TAG = f"base-{_ERA_HI}_to_{_ERA_LO}"

_GO_IMAGE = "golang:1.25.10-bookworm"
_GO_VERSION = "go" + _GO_IMAGE.split(":", 1)[1].split("-", 1)[0]

_MODULE_ROOT = "github.com/go-task/task/v3"
_MODULE_PREFIX = _MODULE_ROOT + "/"
_GO_TEST_TIMEOUT = "30m"
_FLAKY_TESTS = ("TestParams", "TestDeps")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.M)

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


def _test_packages(pr: PullRequest) -> list[str]:
    packages: list[str] = []
    for path in _DIFF_HEADER_RE.findall(pr.test_patch or ""):
        if not path.endswith("_test.go"):
            continue
        directory = posixpath.dirname(path)
        package = f"./{directory}" if directory else "."
        if package not in packages:
            packages.append(package)
    if not packages:
        raise ValueError(
            f"{_ORG}/{_REPO} PR #{pr.number}: test patch touches no _test.go file"
        )
    return sorted(packages)


def _select_packages(pr: PullRequest) -> str:
    return f"""\
pkgs=()
for p in {" ".join(_test_packages(pr))}; do
  if compgen -G "$p/*.go" > /dev/null; then
    pkgs+=("$p")
  else
    echo "note: $p has no .go files in this tree, skipped" >&2
  fi
done
if [ "${{#pkgs[@]}}" -eq 0 ]; then
  echo "FATAL: none of the test-patch packages exist in this tree" >&2
  exit 1
fi"""


def _skip_flag(pr: PullRequest) -> str:
    names = [n for n in _FLAKY_TESTS if f"func {n}(" not in (pr.test_patch or "")]
    if not names:
        return ""
    return f" -skip '^({'|'.join(names)})$'"


def _prepare_sh(pr: PullRequest) -> str:
    return f"""\
#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/{pr.repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {pr.base.sha}
test "$(git rev-parse HEAD)" = "{pr.base.sha}"
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION the era's dependencies ----------
# go.sum at this commit pins every module; GOTOOLCHAIN=local (base ENV) means a
# go.mod newer than the image's toolchain fails here instead of downloading one.
test "$(go env GOVERSION)" = "{_GO_VERSION}"
# proxy.golang.org fetches can die mid-transfer (DNS timeout, truncated zip);
# go verifies every download against go.sum, so a retry is safe. All attempts
# failing still fails the build.
for attempt in 1 2 3 4 5; do
  if go mod download; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    echo "FATAL: go mod download failed after $attempt attempts" >&2
    exit 1
  fi
  sleep $((attempt * 10))
done
go build ./...

# ---------- Section 3: HARD GATE ----------
# Compiles (never runs) every test binary the graded scripts will run.
{_select_packages(pr)}
go test -count=1 -vet=off -run '^$' -exec /bin/true "${{pkgs[@]}}"
echo DEPS_OK
"""


def _run_sh(pr: PullRequest, apply_line: str) -> str:
    return f"""\
#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
{apply_line}
{_select_packages(pr)}

rc=0
go test -json -count=1 -vet=off -timeout={_GO_TEST_TIMEOUT}{_skip_flag(pr)} "${{pkgs[@]}}" 2>&1 | tee /tmp/go-test.log || rc=$?
if [ "$rc" -ne 0 ] && ! grep -q '"Test":"' /tmp/go-test.log; then
  echo "FATAL: go test exited $rc and reported no test events" >&2
fi
exit "$rc"
"""


def _is_repo_package(name: str) -> bool:
    return name == _MODULE_ROOT or name.startswith(_MODULE_PREFIX)


def _package_label(name: str) -> str:
    if name.startswith(_MODULE_PREFIX):
        return name[len(_MODULE_PREFIX) :]
    return "."


class TaskImageBase_TASK_2898_TO_2847(Image):
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
        dockerfile = f"""# syntax=docker/dockerfile:1.6

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

RUN git --version && \\
    test "$(go env GOVERSION)" = "{_GO_VERSION}" && \\
    test -f /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""
        return _BLANK_RUN_RE.sub("\n\n", dockerfile)


class TaskImageDefault_TASK_2898_TO_2847(Image):
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
        return TaskImageBase_TASK_2898_TO_2847(self.pr, self.config)

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
                _run_sh(self.pr, "git apply --whitespace=nowarn /home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_sh(
                    self.pr,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
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


@Instance.register(_ORG, _ERA_FILE)
@Instance.register(_ORG, f"{_ERA_LO}-{_ERA_HI}")
@Instance.register(_ORG, _ERA_KEY)
class TASK_2898_TO_2847(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return TaskImageDefault_TASK_2898_TO_2847(self.pr, self._config)

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
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            package = event.get("Package") or ""
            if action not in buckets or not _is_repo_package(package):
                continue

            label = _package_label(package)
            test = event.get("Test")
            if test:
                buckets[action].add(f"{label}::{test}")
            elif action == "fail" and event.get("FailedBuild"):
                failed.add(f"{label}::[build failed]")

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


def _owns(pr: PullRequest) -> bool:
    if getattr(pr, "org", "") != _ORG or getattr(pr, "repo", "") != _REPO:
        return False
    try:
        number = int(getattr(pr, "number", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not _ERA_LO <= number <= _ERA_HI:
        return False
    interval = str(getattr(pr, "number_interval", "") or "")
    return interval in ("", str(number))


_HOOK_FLAG = f"_{_ERA_FILE}_route_hook"

if not getattr(Instance, _HOOK_FLAG, False):
    _previous_create = Instance.create.__func__

    def _era_create(cls, pr, config, *args, **kwargs):
        if _owns(pr):
            return TASK_2898_TO_2847(pr, config, *args, **kwargs)
        return _previous_create(cls, pr, config, *args, **kwargs)

    Instance.create = classmethod(_era_create)
    setattr(Instance, _HOOK_FLAG, True)
