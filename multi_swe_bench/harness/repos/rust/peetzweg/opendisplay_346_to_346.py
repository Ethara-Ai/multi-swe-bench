import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "opendisplay_346_to_346"

# Deviation from the upstream build tool (documented on purpose):
#   * Upstream: XcodeGen generates the Xcode project from project.yml and CI runs
#     `xcodebuild test` on a macOS runner over the full OpenSidecarMacTests bundle
#     (10 MacTests suites, Apple Foundation/AppKit/CoreGraphics/Network).
#   * Here: xcodebuild and Apple's SDKs cannot run in a Linux container, so the
#     config generates a SwiftPM wrapper package (/home/xctest-pkg) that symlinks a
#     Foundation-only subset of the sources -- the 5 units in _UNIT_SOURCES and 4 of
#     the 10 MacTests suites in _TEST_SOURCES (the test.patch suite plus the suites of
#     the units fix.patch changes) -- and runs them with `swift build` / `swift test`
#     against swift-corelibs-foundation + swift-corelibs-xctest.
#   * The other 6 suites exercise units that import AppKit / CoreGraphics / Network and
#     are out of scope. The graded subset uses only Foundation API that
#     swift-corelibs-foundation provides; any behavioural difference between corelibs and
#     Apple Foundation is a residual risk accepted for this Linux substitute.

# The graded test is MacTests/DesktopPolicyTests.swift (XCTest). Upstream runs it with
# `xcodebuild test` on macos-26 (.github/workflows/tests.yml) against the hostless
# OpenSidecarMacTests bundle, whose sources project.yml lists explicitly. The units under
# test here import only Foundation, so they are compiled on Linux with SwiftPM +
# swift-corelibs-xctest instead of Xcode. Toolchain: Swift 6.3.1, the Swift that the
# macos-26 runner's default Xcode (26.4) shipped with at the base commit (2026-10-01);
# project.yml sets SWIFT_VERSION 5.9, so the package builds in Swift 5 language mode.
# Official Docker library image via its ECR Public mirror (multi-arch, no Hub rate limit).
_SWIFT_IMAGE = "public.ecr.aws/docker/library/swift:6.3.1-bookworm"

# Foundation-only subset of project.yml's OpenSidecarMacTests sources (the rest import
# AppKit / CoreGraphics / Network, which do not exist on Linux) ...
_UNIT_SOURCES = [
    "Mac/DesktopPolicy.swift",
    "Mac/StreamConfiguration.swift",
    "Mac/VirtualCanvasSizing.swift",
    "Mac/PhoneInfo.swift",
    "Shared/Protocol.swift",
]
# ... and the XCTest suites exercising exactly those units: test.patch's suite plus the
# suites of the units fix.patch changes (VirtualCanvasSizing, StreamConfiguration).
_TEST_SOURCES = [
    "MacTests/DesktopPolicyTests.swift",
    "MacTests/VirtualCanvasSizingTests.swift",
    "MacTests/StreamConfigurationTests.swift",
    "MacTests/PhoneInfoTests.swift",
]

_BUILD_TIMEOUT = "1800"
_RUN_TIMEOUT = "900"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_CLASS_MARK = re.compile(r"^@@XCTEST_CLASS (\w+) (\S+)$")
_SYNTH = re.compile(r"^@@SYNTH_FAIL (\S+)$")
_CASE = re.compile(r"^Test Case '(?:[\w.]+\.)?(\w+)[./ ](\w+)\]?' (passed|failed|skipped)\b")

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION ----------
# The test bundle has no package dependencies (Sparkle is only linked by the app).
# The SwiftPM wrapper lives outside the repo, in /home/xctest-pkg, and only symlinks
# the listed sources, so the repo tree stays untouched.
python3 /home/xctest_run.py --setup

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# The unit sources and their XCTest suites compile and link at the base commit, which
# also warms the .build cache the graded runs reuse.
cd /home/xctest-pkg
swift build --build-tests -j 2
swift test --skip-build --list-tests > /dev/null
echo DEPS_OK
"""

# Builds the SwiftPM wrapper and runs its XCTest suites. If the suites do not compile at
# this stage (test.patch uses API that fix.patch adds), every test method whose body
# test.patch touched is reported failed, so the compile break is graded as a failure.
_XCTEST_RUN = r'''import os
import re
import subprocess
import sys

REPO = "/home/[[REPO]]"
PKG = "/home/xctest-pkg"
UNITS = "[[UNIT_SOURCES]]".split()
TESTS = "[[TEST_SOURCES]]".split()
BUILD_TIMEOUT = [[BUILD_TIMEOUT]]
RUN_TIMEOUT = [[RUN_TIMEOUT]]

MANIFEST = """// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "OpenSidecarMacTestsLinux",
    targets: [
        .testTarget(name: "OpenSidecarMacTests", path: "Tests/OpenSidecarMacTests"),
    ]
)
"""

CLASS = re.compile(r"^\s*(?:final\s+)?class\s+(\w+)\s*:\s*XCTestCase\b")
FUNC = re.compile(r"^\s*(?:@\w+\s+)*(?:override\s+)?func\s+(test\w*)\s*\(")


def setup():
    tdir = os.path.join(PKG, "Tests", "OpenSidecarMacTests")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(PKG, "Package.swift"), "w") as fh:
        fh.write(MANIFEST)
    for name in os.listdir(tdir):
        os.unlink(os.path.join(tdir, name))
    for rel in UNITS + TESTS:
        src = os.path.join(REPO, rel)
        if os.path.exists(src):
            os.symlink(src, os.path.join(tdir, rel.replace("/", "__")))
        else:
            print("@@MISSING_SOURCE " + rel, flush=True)


def run(cmd, timeout):
    print("+ " + " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, cwd=PKG, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print("SWIFT_TIMEOUT " + " ".join(cmd), flush=True)
        return 124


def touched_lines():
    rows = {}
    with open("/home/test_targets.txt") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) == 2 and parts[1].isdigit():
                rows.setdefault(parts[0], set()).add(int(parts[1]))
    return rows


def code(line):
    line = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)
    return line.split("//", 1)[0]


def methods(rel):
    try:
        with open(os.path.join(REPO, rel)) as fh:
            lines = fh.read().split("\n")
    except OSError:
        return []
    found = []
    cls = None
    for idx, raw in enumerate(lines, start=1):
        m = CLASS.match(raw)
        if m:
            cls = m.group(1)
        f = FUNC.match(raw)
        if f and cls:
            level = 0
            opened = False
            end = idx
            for j in range(idx - 1, len(lines)):
                c = code(lines[j])
                level += c.count("{") - c.count("}")
                if "{" in c:
                    opened = True
                if opened and level <= 0:
                    end = j + 1
                    break
            found.append((cls, f.group(1), idx, end))
    return found


def announce():
    for rel in TESTS:
        seen = set()
        for cls, _, _, _ in methods(rel):
            if cls not in seen:
                seen.add(cls)
                print("@@XCTEST_CLASS %s %s" % (cls, rel), flush=True)


def synth():
    touched = touched_lines()
    for rel in TESTS:
        lines = touched.get(rel, set())
        for cls, name, start, end in methods(rel):
            if any(start <= n <= end for n in lines):
                print("@@SYNTH_FAIL %s::%s.%s" % (rel, cls, name), flush=True)


def main():
    setup()
    if "--setup" in sys.argv:
        return 0
    announce()
    if run(["swift", "build", "--build-tests", "-j", "2"], BUILD_TIMEOUT) != 0:
        print("@@BUILD_FAILED", flush=True)
        synth()
        return 1
    rc = run(["swift", "test", "--skip-build"], RUN_TIMEOUT)
    print("@@SWIFT_TEST_EXIT %d" % rc, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
python3 /home/xctest_run.py
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _touched_lines(test_patch: str) -> str:
    """New-side line numbers each test.patch hunk adds or sits next to a removal."""
    rows: dict[str, None] = {}
    path: str | None = None
    new_line = 0
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith(".swift") else None
            continue
        if path is None:
            continue
        hunk = _HUNK.match(line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            rows[f"{path}\t{new_line}"] = None
            new_line += 1
        elif line.startswith("-"):
            rows[f"{path}\t{new_line}"] = None
        elif line.startswith(" ") or line == "":
            new_line += 1
    return "".join(f"{row}\n" for row in rows)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[UNIT_SOURCES]]", " ".join(_UNIT_SOURCES))
        .replace("[[TEST_SOURCES]]", " ".join(_TEST_SOURCES))
        .replace("[[BUILD_TIMEOUT]]", _BUILD_TIMEOUT)
        .replace("[[RUN_TIMEOUT]]", _RUN_TIMEOUT)
        .replace("[[REPO]]", pr.repo)
    )


class Opendisplay346To346ImageBase(Image):
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
        return _SWIFT_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_SWIFT_IMAGE}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates build-essential python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    swift --version

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Opendisplay346To346ImageDefault(Image):
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
        return Opendisplay346To346ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test_targets.txt", _touched_lines(pr.test_patch)),
            File(".", "xctest_run.py", _render(_XCTEST_RUN, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        repo = self.pr.repo
        sha = self.pr.base.sha
        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test -z "$(git reflog)"; \\
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


@Instance.register("peetzweg", _INTERVAL)
class OPENDISPLAY_346_TO_346(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return Opendisplay346To346ImageDefault(self.pr, self._config)

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
        # swift-corelibs-xctest: "Test Case 'Class.testName' passed (0.001 seconds)".
        # Names: <test file>::<Class>.<testName>, the file taken from the
        # @@XCTEST_CLASS map the runner prints (timings are never part of the name).
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        files: dict[str, str] = {}
        for raw in clean.splitlines():
            line = raw.strip()
            mark = _CLASS_MARK.match(line)
            if mark:
                files[mark.group(1)] = mark.group(2)
                continue
            synth = _SYNTH.match(line)
            if synth:
                failed_tests.add(synth.group(1))
                continue
            case = _CASE.match(line)
            if case:
                cls, method, status = case.group(1), case.group(2), case.group(3)
                name = f"{files.get(cls, cls)}::{cls}.{method}"
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
