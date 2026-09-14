from __future__ import annotations

import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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


_APPLY_PATCH_SH = """#!/bin/bash
set -e
cd /home/ko
EXCL=/tmp/excl.$$
restore_binaries() {
  local patch="$1" path="" new=""
  : > "$EXCL"
  while IFS= read -r line; do
    case "$line" in
      "diff --git "*) path="${line#*" b/"}" ;;
      "index "*)      new="${line#*..}"; new="${new%% *}" ;;
      "Binary files "*)
        printf -- '--exclude=%s\\n' "$path" >> "$EXCL"
        if [[ "$new" =~ ^0+$ ]]; then rm -f "$path"
        elif git cat-file -e "$new" 2>/dev/null; then
          mkdir -p "$(dirname "$path")"; git cat-file blob "$new" > "$path"
        else
          echo "apply_patch: WARNING blob $new for $path not available"
        fi ;;
    esac
  done < "$patch"
}
for patch in "$@"; do
  restore_binaries "$patch"
  EX=()
  if [ -s "$EXCL" ]; then mapfile -t EX < "$EXCL"; fi
  if ! git apply --whitespace=nowarn "${EX[@]}" "$patch" 2>/tmp/apply.err; then
    echo "plain git apply failed for $(basename "$patch"), retrying with --3way:"
    cat /tmp/apply.err
    git add -A >/dev/null 2>&1 || true
    git apply --3way --whitespace=nowarn "${EX[@]}" "$patch"
    echo "applied via --3way"
  fi
  git add -A >/dev/null 2>&1 || true
done
rm -f "$EXCL"
"""


_GO_TEST_REPORT_PY = '''"""Turn one `go test -json` stream into per-test results.

usage: go_test_report.py <events.json>

    go:pkg/build > TestGoBuildQualifyImport PASSED

Exit 0 all passed, 1 at least one failed, 2 the stream contained no tests at all
(the runner never started -- the case FLOW GATE 0 exists to catch).
"""
import json
import sys

MODULE = "github.com/google/ko/"

STATUS = {"pass": "PASSED", "fail": "FAILED", "skip": "SKIPPED"}


def main():
    path = sys.argv[1]
    results, pkgs_with_tests, pkg_level = {}, set(), {}

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write("go_test_report: cannot read %s: %s\\n" % (path, exc))
        return 2

    with fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("Action") not in STATUS:
                continue
            pkg = ev.get("Package", "")
            if pkg.startswith(MODULE):
                pkg = pkg[len(MODULE):]
            test = ev.get("Test")
            if not test:
                # PACKAGE-level event: recorded for the stderr summary ONLY.
                pkg_level[pkg] = ev["Action"]
                continue
            results[(pkg, test)] = ev["Action"]
            pkgs_with_tests.add(pkg)

    for pkg, action in sorted(pkg_level.items()):
        if action == "fail" and pkg not in pkgs_with_tests:
            sys.stderr.write(
                "go_test_report: PACKAGE PRODUCED NO TESTS (build failure?): %s\\n" % pkg)

    sys.stderr.write(
        "go_test_report: packages with tests=%d | package-level events=%d | tests=%d\\n"
        % (len(pkgs_with_tests), len(pkg_level), len(results)))

    if not results:
        sys.stderr.write(
            "go_test_report: the run reported no tests; the runner never started\\n")
        return 2

    failed = False
    for (pkg, test), action in sorted(results.items()):
        print("go:%s > %s %s" % (pkg, test, STATUS[action]))
        if action == "fail":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
'''


_RUN_TESTS_SH = """#!/bin/bash
set -uo pipefail
cd /home/ko
JSON=/tmp/go-test.json
rm -f "$JSON"
go test -json -count=1 -timeout=15m ./... > "$JSON" 2>/tmp/go-test.err
go_rc=$?
echo "go test exit=${go_rc}"
echo "----- go test stderr -----"
cat /tmp/go-test.err
echo "----- per-test results -----"
python3 /home/go_test_report.py "$JSON"
"""


class KoImageBase(Image):

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
        return "golang:1.16"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def extra_packages(self) -> list[str]:
        return []

    def extra_setup(self) -> str:
        return (
            "RUN go env -w CGO_ENABLED=0 GOFLAGS=-mod=mod && \\\n"
            "    go version && \\\n"
            "    go mod download && \\\n"
            "    go build ./... && \\\n"
            "    test -d /go/pkg/mod/github.com && \\\n"
            "    python3 --version"
        )


class KoImageDefault(Image):

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
        return KoImageBase(self.pr, self._config)

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
            File(".", "go_test_report.py", _GO_TEST_REPORT_PY),
            File(".", "run_tests.sh", _RUN_TESTS_SH),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
test "$(git rev-parse HEAD)" = "$(git rev-parse {pr.base.sha})"
git clean -fdq
bash /home/check_git_changes.sh
go mod download || true
go version
python3 --version
test "$(go env CGO_ENABLED)" = "0"
test -d /go/pkg/mod/github.com
git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/apply_patch.sh /home/test.patch
bash /home/run_tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/ko
bash /home/apply_patch.sh /home/test.patch /home/fix.patch
bash /home/run_tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {image_name}

{copies}
RUN bash /home/prepare.sh

"""


_GO_JSON_MIN_PR = 1307
_GO_IMAGE = "golang:1.21-bookworm"
_MODULE_PREFIX = "github.com/google/ko/"
_TEST_CMD = "go test -json -count=1 -timeout=20m ./... 2>&1"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class KoGoModImageBase(Image):
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
        return "base"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
ARG BASE_COMMIT

{DockerfileEnhancer._PROXY_ARGS}

{DockerfileEnhancer._ENV_BLOCK}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{DockerfileEnhancer._CERT_SYMLINKS}

{self.global_env}

RUN printf 'Acquire::Check-Valid-Until "false";\\nAcquire::Retries "5";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

ENV GOTOOLCHAIN=local

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class KoGoModImageDefault(Image):
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
        return KoGoModImageBase(self.pr, self.config)

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
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

cd /home/{self.pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

go version
go env GOTOOLCHAIN GOPATH GOMODCACHE

go mod download || true
go build ./... || {{ echo "prepare.sh: go build failed at the base commit"; exit 1; }}
go test -count=1 -run '^$' ./... || {{ echo "prepare.sh: a test package failed to compile at the base commit"; exit 1; }}
git checkout -- .
git clean -fdq
bash /home/check_git_changes.sh
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch
{_TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -eo pipefail

cd /home/{self.pr.repo}
export CI=true
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError(
                "KoGoModImageDefault dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT={self.pr.base.sha}
ENV BASE_COMMIT=${{BASE_COMMIT}}

{copy_commands}

WORKDIR /home/{self.pr.repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


def _parse_go_json_log(test_log: str) -> TestResult:
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

        test = event.get("Test")
        action = event.get("Action")
        if not test or action not in buckets:
            continue

        package = event.get("Package") or ""
        if package.startswith(_MODULE_PREFIX):
            package = package[len(_MODULE_PREFIX) :]
        elif package == _MODULE_PREFIX.rstrip("/"):
            package = "."

        buckets[action].add(f"{package}::{test}")

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


@Instance.register("ko-build", "ko")
class Ko(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number >= _GO_JSON_MIN_PR:
            return KoGoModImageDefault(self.pr, self._config)
        return KoImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        if self.pr.number >= _GO_JSON_MIN_PR:
            return _parse_go_json_log(test_log)
        test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        marker = "----- per-test results -----"
        if marker in test_log:
            test_log = test_log.rsplit(marker, 1)[1]

        passed_tests, failed_tests, skipped_tests = set(), set(), set()

        result_res = [
            (re.compile(r"^(.+?)\s+PASSED$"), "pass"),
            (re.compile(r"^(.+?)\s+FAILED$"), "fail"),
            (re.compile(r"^(.+?)\s+SKIPPED$"), "skip"),
        ]

        for line in test_log.splitlines():
            line = line.strip()
            for rx, kind in result_res:
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                if kind == "pass":
                    if name not in failed_tests:
                        passed_tests.add(name)
                elif kind == "fail":
                    failed_tests.add(name)
                    passed_tests.discard(name)
                else:
                    skipped_tests.add(name)
                break

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
