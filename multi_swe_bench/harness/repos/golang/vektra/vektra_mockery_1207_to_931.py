import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# vektra/mockery PRs #931 - #1207: the v3 era. Every PR in this range targets
# the `v3` branch, and every base commit has the same layout:
#   go.mod   module github.com/vektra/mockery/v3, `go` 1.23 .. 1.26.0
#   go.work  use ( . ./internal/fixtures/example_project/pkg_with_submodules
#                  ./tools )   (task and gotestsum come from ./tools)
#   e2e/     run_all.sh + test_*.sh scripts (+ test_*/run.sh dirs and Go
#            packages later in the era)
# Go is backward compatible with older `go` directives, so one Go 1.26 base
# (GOTOOLCHAIN=local, no toolchain download) builds the whole era.
#
# Test surface mirrors Taskfile.yml `test` + `test.e2e`:
#   1. go test over every package of the main module outside e2e/ (the
#      package list in Taskfile `test` grew over the era, from ./... to
#      ./config ./internal ./template ./template_funcs; all of them are inside
#      this set);
#   2. each e2e script, run the way the tree's own e2e/run_all.sh runs it, but
#      one at a time so a failing script does not hide the ones after it;
#   3. go test over the e2e Go packages.
# Unit and e2e Go tests run in workspace mode (go.work), as `go run` of task
# and gotestsum does in CI.
_INTERVAL_NAME = "vektra_mockery_1207_to_931"
_LO, _HI = 931, 1207

_GO_IMAGE = "golang:1.26.11-bookworm"

# The fix patch of #931 also adds a compiled `mockery` binary at the repo root
# (committed by accident; `Binary files /dev/null and b/mockery differ`, no
# full index line), which git cannot apply. No test reads it. The pattern only
# matches that root path, never internal/... sources.
_GIT_APPLY = "git apply --whitespace=nowarn --exclude=mockery"
_MODULE = "github.com/vektra/mockery/v3"

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

# Reads `go test -json` on stdin, prints one stable line per result:
#   MSWB_GO <PASS|FAIL|SKIP> <pkg>::<Test>/<subtest>
#   MSWB_GO_PKG <PASS|FAIL|SKIP> <pkg>    (package aggregate, not graded)
#   MSWB_GO_BUILD_FAIL <pkg>              (diagnostic, not graded)
# <pkg> is the import path without the module prefix ("." for the root
# package). A test that started but never reported (panic, timeout) is FAIL.
# Output of failing tests and failed builds is echoed, capped, for debugging.
# No durations, so names are identical across stages.
_GO_REPORT_PY = r'''import json
import sys

MODULE_PREFIX = "github.com/vektra/mockery/v3"
MAX_LINES_PER_TEST = 40
STATUS = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}

dump_budget = [20000]
any_fail = [False]
open_tests = {}
output = {}
build_output = {}


def pkg_name(p):
    if not p:
        return "<unknown>"
    if p == MODULE_PREFIX:
        return "."
    if p.startswith(MODULE_PREFIX + "/"):
        return p[len(MODULE_PREFIX) + 1:]
    return p


def clean(s):
    return " ".join(str(s).split())


def out(line):
    sys.stdout.write(line + "\n")


def remember(store, key, line):
    lines = store.setdefault(key, [])
    if len(lines) < MAX_LINES_PER_TEST:
        lines.append(line)


def emit(status, name):
    if status == "FAIL":
        any_fail[0] = True
    out(f"MSWB_GO {status} {name}")


def dump(lines):
    for line in lines or []:
        if dump_budget[0] <= 0:
            return
        dump_budget[0] -= 1
        out("    | " + str(line).rstrip("\n"))


for raw in sys.stdin:
    try:
        e = json.loads(raw)
    except ValueError:
        if raw.strip():
            out("MSWB_GO_RAW " + raw.rstrip("\n"))
        continue
    if not isinstance(e, dict):
        continue
    action = e.get("Action")
    if action == "build-output":
        remember(build_output, e.get("ImportPath"), e.get("Output") or "")
        continue
    if action == "build-fail":
        out("MSWB_GO_BUILD_FAIL " + pkg_name(e.get("ImportPath")))
        dump(build_output.get(e.get("ImportPath")))
        continue
    pkg = pkg_name(e.get("Package"))
    test = clean(e["Test"]) if e.get("Test") else ""
    if action == "run" and test:
        open_tests.setdefault(pkg, set()).add(test)
        continue
    if action == "output":
        remember(output, (pkg, test), e.get("Output") or "")
        continue
    status = STATUS.get(action)
    if not status:
        continue
    if test:
        open_tests.get(pkg, set()).discard(test)
        emit(status, f"{pkg}::{test}")
        if status == "FAIL":
            dump(output.get((pkg, test)))
        output.pop((pkg, test), None)
        continue
    for t in sorted(open_tests.pop(pkg, set())):
        emit("FAIL", f"{pkg}::{t}")
    for key in [k for k in output if k[0] == pkg and k[1]]:
        del output[key]
    if status == "FAIL":
        any_fail[0] = True
    out(f"MSWB_GO_PKG {status} {pkg}")
    if status == "FAIL":
        dump(output.get((pkg, "")))
    output.pop((pkg, ""), None)

for pkg in sorted(open_tests):
    for t in sorted(open_tests[pkg]):
        emit("FAIL", f"{pkg}::{t}")
sys.stdout.flush()
sys.exit(1 if any_fail[0] else 0)
'''


def _run_tests_sh(repo: str) -> str:
    # Identical for run.sh / test-run.sh / fix-run.sh. Every suite runs even if
    # an earlier one fails; each failure is recorded and returned at the end.
    # MSWB_SETUP lines are for diagnosis only; parse_log does not grade them.
    return f"""#!/bin/bash
set -eo pipefail

REPO=/home/{repo}
cd "$REPO"
export CI=true
export GOTOOLCHAIN=local
status=0

echo "=== MSWB packages"
all_pkgs=""
if all_pkgs=$(go list -e ./...); then
    echo "MSWB_SETUP PASS go-list"
else
    echo "MSWB_SETUP FAIL go-list"
    status=1
fi
unit_pkgs=()
e2e_pkgs=()
while IFS= read -r p; do
    case "$p" in
        "") ;;
        {_MODULE}/e2e|{_MODULE}/e2e/*) e2e_pkgs+=("$p") ;;
        *) unit_pkgs+=("$p") ;;
    esac
done <<< "$all_pkgs"

echo "=== MSWB suite: unit"
if [ "${{#unit_pkgs[@]}}" -gt 0 ]; then
    if go test -count=1 -timeout=30m -json "${{unit_pkgs[@]}}" 2>&1 | python3 /home/mswb_go_report.py; then
        :
    else
        status=1
    fi
fi

echo "=== MSWB suite: mocks git-state"
# Taskfile `test.ci` runs `task mocks` then `task git-state` (git diff
# --exit-code): regenerating every mock with the code under test must
# reproduce the committed mocks exactly. The tree is staged first so only
# changes made by the regeneration count, not the stage's own patches.
if (
    cd "$REPO" || exit 1
    git add -A || exit 1
    go run github.com/go-task/task/v3/cmd/task mocks || exit 1
    git diff --stat --exit-code || exit 1
    test -z "$(git ls-files --others --exclude-standard)"
); then
    echo "MSWB_E2E PASS ci/task-mocks-git-state"
else
    echo "MSWB_E2E FAIL ci/task-mocks-git-state"
    git status --porcelain | head -40
    status=1
fi
# Leave the tree exactly as the stage's patches left it for the e2e scripts.
git checkout -- .
git clean -fdq
git reset -q

echo "=== MSWB suite: e2e scripts"
# Same discovery as the tree's e2e/run_all.sh: test_*.sh scripts, plus
# test_*/run.sh once run_all.sh handles directories, and the mocks reset
# before every script when run_all.sh does it.
reset_mocks=0
dirs_ok=0
if [ -f e2e/run_all.sh ]; then
    if grep -q 'mocks.remove' e2e/run_all.sh; then reset_mocks=1; fi
    if grep -q 'run.sh' e2e/run_all.sh; then dirs_ok=1; fi
fi
shopt -s nullglob
for t in e2e/test_*; do
    f="$t"
    if [ -d "$t" ]; then
        [ "$dirs_ok" = 1 ] || continue
        f="$t/run.sh"
        [ -f "$f" ] || continue
    elif [[ "$t" != *.sh ]]; then
        continue
    fi
    echo "--- MSWB e2e $f"
    if (
        cd "$REPO" || exit 1
        if [ "$reset_mocks" = 1 ]; then
            go run github.com/go-task/task/v3/cmd/task mocks.remove || exit 1
            go run github.com/go-task/task/v3/cmd/task mocks.generate || exit 1
        fi
        bash "$f"
    ); then
        echo "MSWB_E2E PASS $f"
    else
        echo "MSWB_E2E FAIL $f"
        status=1
    fi
done
shopt -u nullglob

echo "=== MSWB suite: e2e go"
if [ "${{#e2e_pkgs[@]}}" -gt 0 ]; then
    if go test -count=1 -timeout=30m -json "${{e2e_pkgs[@]}}" 2>&1 | python3 /home/mswb_go_report.py; then
        :
    else
        status=1
    fi
fi

echo "=== MSWB done status=$status"
exit "$status"
"""


class VektraMockery1207To931ImageBase(Image):
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

    def image_tag(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def workdir(self) -> str:
        return f"base-{_LO}_to_{_HI}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base for the whole era: keeps full history and never pins or
        # scrubs. The leading syntax directive opts out of DockerfileEnhancer,
        # so all proxy / CA / label infrastructure is written out by hand.
        org = self.pr.org
        repo = self.pr.repo

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{repo} && \\\n'
                f"    cd /home/{repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {repo} /home/{repo}"

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

# GOTOOLCHAIN=local: the image's Go 1.26 builds every `go` directive of the
# era (1.23 .. 1.26.0); never download another toolchain.
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GOTOOLCHAIN=local \\
    CI=true

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt
{global_block}
WORKDIR /home/

# python3 runs the go test -json reporter.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN go version && go env GOTOOLCHAIN

RUN git config --global --add safe.directory '*'

{code}
{clear_block}
CMD ["/bin/bash"]
"""


class VektraMockery1207To931ImageDefault(Image):
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
        return VektraMockery1207To931ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha

        prepare = """#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Section 1: PIN
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh

# Section 2: PROVISION
export CI=true
export GOTOOLCHAIN=local
# Module downloads fail transiently; retry up to three times.
retry() {{
    local n
    for n in 1 2 3; do
        "$@" && return 0
        echo "prepare: attempt $n failed: $*" >&2
        sleep 15
    done
    return 1
}}
test "$(head -n1 go.mod)" = "module {module}"
test -f go.work
retry go mod download
retry bash -c 'cd tools && go mod download'
# Later in the era the Taskfile mocks tasks run mockery with GOWORK=off,
# which resolves the main module on its own (other versions than the
# workspace); when this tree's Taskfile does that, fetch and build that
# resolution too, so no stage depends on the network for modules the base
# tree already needs. Earlier trees never use it (their go.sum alone is not
# complete without go.work.sum).
gowork_off=0
if grep -q 'GOWORK=off go run' Taskfile.yml; then gowork_off=1; fi
if [ "$gowork_off" = 1 ]; then
    retry env GOWORK=off go mod download
    retry bash -c 'cd tools && GOWORK=off go mod download'
fi
# Warm the build cache the stages use: every package and its tests, and the
# task runner the e2e scripts call through `go run`.
go build ./...
if [ "$gowork_off" = 1 ]; then GOWORK=off go build ./...; fi
go test -count=1 -run '^$' ./... > /dev/null
go run github.com/go-task/task/v3/cmd/task --version
# Warming adds checksum lines to go.work.sum; restore every tracked file so
# the stage patches (several edit go.mod / go.sum / go.work.sum) apply to the
# pristine base tree. The module and build caches are kept.
git checkout -- .

# Section 3: GATE
go version
test "$(go env GOTOOLCHAIN)" = "local"
python3 -c 'import json, sys'
go list ./... > /home/go-packages.txt
test -s /home/go-packages.txt
test -f e2e/run_all.sh
echo "DEPS_OK $(go version)"
""".format(repo=repo, sha=sha, module=_MODULE)

        run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
bash /home/mswb_run_tests.sh
""".format(repo=repo)

        test_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
{git_apply} /home/test.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo, git_apply=_GIT_APPLY)

        fix_run = """#!/bin/bash
set -eo pipefail

cd /home/{repo}
export CI=true
{git_apply} /home/test.patch /home/fix.patch
bash /home/mswb_run_tests.sh
""".format(repo=repo, git_apply=_GIT_APPLY)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_go_report.py", _GO_REPORT_PY),
            File(".", "mswb_run_tests.sh", _run_tests_sh(repo)),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        # dependency() returns an Image, so DockerfileEnhancer never touches
        # this file: the pin and the history scrub are written out here.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = "\n".join(
            f"COPY {file.name} /home/" for file in self.files()
        )

        global_env = self.global_env
        clear_env = self.clear_env
        global_block = f"\n{global_env}\n" if global_env else ""
        clear_block = f"\n{clear_env}\n" if clear_env else ""

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}
{global_block}
WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
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
{clear_block}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# MSWB_SETUP / MSWB_GO_PKG / MSWB_GO_BUILD_FAIL / MSWB_GO_RAW lines are
# diagnostics, not tests, and are deliberately not matched.
_RESULT_RE = re.compile(r"^MSWB_(?:GO|E2E) (PASS|FAIL|SKIP) (\S.*?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
    for line in _ANSI_RE.sub("", test_log or "").replace("\r", "").splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.groups()
        buckets[status].add(name)

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


@Instance.register("vektra", _INTERVAL_NAME)
class VEKTRA_MOCKERY_1207_TO_931(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VektraMockery1207To931ImageDefault(self.pr, self._config)

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
        return _parse_mswb_log(test_log)


# ---------------------------------------------------------------------------
# Routing
#
# The JSONL carries no number_interval, so Instance.create() looks up the bare
# "vektra/mockery" key. No other mockery config exists, so this module owns
# that key: PRs of this era go to the era instance, anything else is refused
# rather than built with the wrong toolchain.
# ---------------------------------------------------------------------------


@Instance.register("vektra", "mockery")
class VEKTRA_MOCKERY_1207_TO_931_DISPATCH(Instance):
    """Router only: __new__ returns the era-appropriate instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return VEKTRA_MOCKERY_1207_TO_931(pr, config, *args, **kwargs)
        raise ValueError(
            f"vektra/mockery PR #{pr.number} is outside every configured era "
            f"(only #{_LO}-#{_HI})"
        )
