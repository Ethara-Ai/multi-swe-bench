"""runkids/skillshare, PRs #502-#854 (merged 2026-10-09 .. 2026-11-11), one era.

Era analysis (checked at the earliest and latest base commits, 5e3840ef6f for #502 and
b97206e946 for #854):
  * runtime       go.mod `go 1.25.5` (#502, #577) and `go 1.26.10` (#649 onwards).
                  golang:1.26.11 with GOTOOLCHAIN=local builds both directives without
                  fetching a toolchain (1.26.10 is the floor for the later PRs)
  * layout        one module (go.mod at the root); ui/ is a TypeScript web UI that the
                  Go tests do not build
  * test runner   CI runs `go test ./cmd/skillshare/... ./internal/...` and
                  `go test ./tests/integration/...`, both with SKILLSHARE_TEST_BINARY
                  pointing at `make build`'s bin/skillshare; here one `go test ./...`
                  covers the same packages, and the binary is rebuilt after the patches
                  are applied so the integration tests drive the patched code
Nothing changes across the range, so a single shared base serves every PR.

go_test_report.py turns `go test -json` into one line per test, named
`<package path relative to the module>::<Test[/subtest]>`. A package that fails to build
has no per-test events, so the Test functions the patches add to it are reported as FAIL.
"""

import re
from pathlib import Path

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_ORG = "runkids"
_REPO = "skillshare"
_ERA = Path(__file__).stem
_ERA_MATCH = re.fullmatch(r"skillshare_(\d+)_to_(\d+)", _ERA)
if _ERA_MATCH is None:
    raise ImportError(f"{__file__}: file name must look like skillshare_<hi>_to_<lo>.py")
_ERA_HI = int(_ERA_MATCH.group(1))
_ERA_LO = int(_ERA_MATCH.group(2))
_BASE_TAG = f"base-{_ERA_LO}_to_{_ERA_HI}"

_GO_IMAGE = "golang:1.26.11-bookworm"
_REPO_DIR = f"/home/{_REPO}"
_BINARY = f"{_REPO_DIR}/bin/skillshare"
_TEST_LOG = "/tmp/mswb-go-test.json"

_APPLY = "git apply --whitespace=nowarn"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESULT = re.compile(r"^MSWB_GO (PASS|FAIL|SKIP) (\S.*?)\s*$", re.MULTILINE)

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_GO_TEST_REPORT = r'''#!/usr/bin/env python3
"""Print one line per Go test from `go test -json` output:

    MSWB_GO <PASS|FAIL|SKIP> <package path relative to the module>::<Test[/subtest]>

A package that fails to build (or to start) emits no per-test events. The top-level Test
functions that the applied patches ADD to that package (new *_test.go files, or `func TestX(`
lines added to tracked ones, from git) are reported as FAIL, so a new test that cannot compile
yet is a failure rather than a missing test. Tests that already existed at the base commit are
left unreported, so the harness classifies them from the baseline run instead of crediting
them as fixed.
"""
import json
import os
import re
import subprocess
import sys

log_path, root = sys.argv[1], sys.argv[2]

module = ""
with open(os.path.join(root, "go.mod"), encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("module "):
            module = line.split()[1].strip()
            break


def rel(pkg):
    if pkg == module:
        return "."
    if module and pkg.startswith(module + "/"):
        return pkg[len(module) + 1:]
    return pkg


TEST_FUNC = re.compile(r"^func\s+(Test[A-Z0-9_]\w*|Test)\s*\(\s*\w+\s+\*testing\.T\s*\)", re.M)

results = {}
pkgs_with_events = set()
failed_pkgs = set()
with open(log_path, encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        action, pkg, test = ev.get("Action"), ev.get("Package"), ev.get("Test")
        if pkg and test and action in ("pass", "fail", "skip"):
            results[rel(pkg) + "::" + test] = action.upper()
            pkgs_with_events.add(pkg)
        elif pkg and not test and action == "fail":
            failed_pkgs.add(pkg)

def git(*args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout


def added_tests(pkg_rel):
    """Top-level Test functions the applied patches add to one package directory."""
    pathspec = (pkg_rel + "/") if pkg_rel != "." else "."
    names = set()
    for path in git("ls-files", "--others", "--exclude-standard", "--", pathspec).split():
        if path.endswith("_test.go") and os.path.dirname(path) == ("" if pkg_rel == "." else pkg_rel):
            with open(os.path.join(root, path), encoding="utf-8", errors="replace") as fh:
                names.update(TEST_FUNC.findall(fh.read()))
    diff = git("diff", "--unified=0", "HEAD", "--", pathspec)
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
            ok = path and path.endswith("_test.go") and os.path.dirname(path) == ("" if pkg_rel == "." else pkg_rel)
            current = path if ok else None
        elif current and line.startswith("+") and not line.startswith("+++"):
            names.update(TEST_FUNC.findall(line[1:]))
    return names


for pkg in sorted(failed_pkgs - pkgs_with_events):
    names = added_tests(rel(pkg))
    for name in sorted(names):
        results.setdefault(rel(pkg) + "::" + name, "FAIL")
    print("MSWB_GO_PKG_FAIL %s (build failed; %d patch-added tests reported as FAIL)" % (rel(pkg), len(names)))

for name in sorted(results):
    print("MSWB_GO %s %s" % (results[name], name))
counts = {s: sum(1 for v in results.values() if v == s) for s in ("PASS", "FAIL", "SKIP")}
print("MSWB_GO_SUMMARY pass=%d fail=%d skip=%d failed_packages=%d"
      % (counts["PASS"], counts["FAIL"], counts["SKIP"], len(failed_pkgs)))
'''


def _sections(*parts: str) -> str:
    return "\n\n".join(filter(None, parts)) + "\n"


def _go_env() -> str:
    return "\n".join(
        (
            "export CI=true",
            "export GOTOOLCHAIN=local",
            "export CGO_ENABLED=1",
            "export GOFLAGS=-buildvcs=false",
            f"export SKILLSHARE_TEST_BINARY={_BINARY}",
        )
    )


def _prepare_script(pr: PullRequest) -> str:
    sha = pr.base.sha
    return _sections(
        "#!/bin/bash\nset -euo pipefail",
        _go_env(),
        f"""cd {_REPO_DIR}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
test "$(git rev-parse HEAD)" = "{sha}"
bash /home/check_git_changes.sh""",
        f"""go version
go mod download
go build ./...
mkdir -p bin
go build -o {_BINARY} ./cmd/skillshare""",
        f"""test "$(go env GOTOOLCHAIN)" = "local"
test -x {_BINARY}
go test -count=1 -run '^$' ./... > /dev/null
echo "prepare: every package and test binary compiles"
echo DEPS_OK""",
    )


def _run_script(apply: str = "") -> str:
    setup = "\n".join(filter(None, (f"cd {_REPO_DIR}", apply)))
    build = f"""# The integration tests run this binary: rebuild it from the patched tree. A build
# failure is reported here and surfaces as failing integration tests below.
mkdir -p bin
if ! go build -o {_BINARY} ./cmd/skillshare; then
    echo "MSWB_GO_BINARY_BUILD_FAILED {_BINARY}"
fi"""
    test = f"""if go test -count=1 -p 4 -timeout 20m -json ./... > {_TEST_LOG} 2>&1; then
    status=0
else
    status=$?
fi
python3 /home/go_test_report.py {_TEST_LOG} {_REPO_DIR}
exit "$status\""""
    return _sections(
        "#!/bin/bash\nset -eo pipefail",
        _go_env(),
        setup,
        build,
        test,
    )


class SkillshareImageBase(Image):
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
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        header = f"""# syntax=docker/dockerfile:1.6

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
    CGO_ENABLED=1 \\
    GOFLAGS=-buildvcs=false

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

# python3 runs go_test_report.py, the `go test -json` reporter.
RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates build-essential python3 && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*' && \\
    go version && go env GOTOOLCHAIN && git --version && python3 --version"""
        clone = f"""WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null"""
        return _sections(
            header,
            self.global_env,
            clone,
            self.clear_env,
            'CMD ["/bin/bash"]',
        )


class SkillshareImageDefault(Image):
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
        return SkillshareImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "go_test_report.py", _GO_TEST_REPORT),
            File(".", "prepare.sh", _prepare_script(self.pr)),
            File(".", "run.sh", _run_script()),
            File(".", "test-run.sh", _run_script(f"{_APPLY} /home/test.patch")),
            File(".", "fix-run.sh", _run_script(f"{_APPLY} /home/test.patch /home/fix.patch")),
        ]

    def dockerfile(self) -> str:
        names = " ".join(file.name for file in self.files())
        return _sections(
            f"FROM {self.dependency().image_full_name()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
            self.global_env,
            f"COPY {names} /home/",
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            Image._HARDENING_BLOCK.strip("\n"),
            self.clear_env,
        )


@Instance.register(_ORG, _REPO)
@Instance.register(_ORG, _ERA)
class SKILLSHARE_854_TO_502(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        if not _ERA_LO <= pr.number <= _ERA_HI:
            raise ValueError(
                f"{_ORG}/{_REPO} PR #{pr.number} is outside era {_ERA} ({_ERA_LO}-{_ERA_HI})"
            )
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SkillshareImageDefault(self.pr, self._config)

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
        test_log = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for status, name in _RESULT.findall(test_log):
            if status == "PASS":
                passed_tests.add(name)
            elif status == "FAIL":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        skipped_tests -= passed_tests
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
