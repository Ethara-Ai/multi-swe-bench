import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_RUST_IMAGE = "rust:1.42.0-buster"
_INTERVAL_NAME = "druid_818_to_765"
_BASE_TAG = "base-818_to_765"
_PACKAGES = "druid druid-shell druid-derive"

_INFRA_BLOCK = """ARG TARGETARCH
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
"""

_LANG_ENV = """ENV CI=true \\
    CARGO_TERM_COLOR=never \\
    CARGO_NET_RETRY=5
"""

_APT_RUN = """RUN printf 'deb http://archive.debian.org/debian buster main\\ndeb http://archive.debian.org/debian-security buster/updates main\\n' > /etc/apt/sources.list \\
    && apt-get update \\
    && apt-get install -y --no-install-recommends git ca-certificates python3 pkg-config libgtk-3-dev \\
    && rm -rf /var/lib/apt/lists/*
"""

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

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "+${{BASE_COMMIT}}:refs/heads/msb-base-commit"
git checkout --detach "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

export CARGO_BUILD_JOBS=2
rustc --version
cargo --version
python3 /home/mswb_vendor.py
cargo test --locked {pkg_flags} --lib --tests --no-run
pkg-config --exists gtk+-3.0 && test -n "$(find target/debug/deps -maxdepth 1 -type f -perm -u+x -name 'druid-*')" && echo DEPS_OK
"""

_TARGETS_SH = """#!/bin/bash
set -eo pipefail
dir="$1"
if [ -f "$dir/src/lib.rs" ]; then
  echo lib
  echo doc
fi
if [ -d "$dir/tests" ]; then
  for f in $(find "$dir/tests" -maxdepth 1 -name '*.rs' | sort); do
    echo "test/$(basename "$f" .rs)"
  done
fi
"""

_RUN_TESTS_SH = """#!/bin/bash
export CI=true
export CARGO_TERM_COLOR=never
export CARGO_BUILD_JOBS=2
cd /home/{repo}
mkdir -p /tmp/mswb_logs
for pkg in {packages}; do
  [ -d "$pkg" ] || continue
  for target in $(bash /home/mswb_targets.sh "$pkg"); do
    case "$target" in
      lib) flag="--lib" ;;
      doc) flag="--doc" ;;
      test/*) flag="--test ${{target#test/}}" ;;
    esac
    log="/tmp/mswb_logs/${{pkg}}_${{target//\\//_}}.log"
    echo "::mswb::target ${{pkg}} ${{target}}"
    cargo test -p "$pkg" $flag > "$log" 2>&1
    echo "::mswb::exit $?"
    python3 /home/mswb_digest.py "$pkg" "$target" "$log"
  done
done
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
bash /home/run_tests.sh
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_VENDOR_PY = r"""import subprocess
import sys
import time

for attempt in range(5):
    if subprocess.call(["cargo", "fetch", "--locked"]) == 0:
        sys.exit(0)
    time.sleep(30)
sys.exit(1)
"""

_DIGEST_PY = r"""import os
import re
import sys

pkg, target, log_path = sys.argv[1], sys.argv[2], sys.argv[3]
kind = "doc" if target == "doc" else ("lib" if target == "lib" else target)
prefix = "cargo::{}::{}::".format(pkg, kind)

with open(log_path, errors="replace") as fh:
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", fh.read())

line_re = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)\s*$")
results = {}
groups = {}
for line in text.splitlines():
    m = line_re.match(line.strip())
    if not m:
        continue
    raw_name = re.sub(r" - should panic$", "", m.group(1))
    lm = re.search(r" \(line (\d+)\)$", raw_name)
    base_name = raw_name[: lm.start()] if lm else raw_name
    line_no = int(lm.group(1)) if lm else 0
    status = {"ok": "PASSED", "FAILED": "FAILED", "ignored": "SKIPPED"}[m.group(2)]
    groups.setdefault(base_name, {})
    if groups[base_name].get(line_no) != "FAILED":
        groups[base_name][line_no] = status
for base_name, by_line in groups.items():
    for idx, line_no in enumerate(sorted(by_line)):
        name = base_name if idx == 0 else "%s #%d" % (base_name, idx + 1)
        results[name] = by_line[line_no]

ran = re.search(r"^running \d+ tests?$", text, re.M) is not None


def module_of(root, path):
    rel = os.path.relpath(path, root)[:-3].split(os.sep)
    if rel[-1] in ("lib", "main", "mod"):
        rel = rel[:-1]
    return rel


def scan(path, base):
    found = []
    stack = []
    depth = 0
    pending = False
    with open(path, errors="replace") as fh:
        for raw in fh:
            line = raw.split("//")[0]
            mm = re.match(r"\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z0-9_]+)\s*\{", line)
            if mm:
                stack.append((mm.group(1), depth))
            if re.match(r"\s*#\[test\]", line):
                pending = True
            fm = re.match(r"\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)", line)
            if fm and pending:
                found.append("::".join(base + [s[0] for s in stack] + [fm.group(1)]))
                pending = False
            depth += line.count("{") - line.count("}")
            while stack and depth <= stack[-1][1]:
                stack.pop()
    return found


if not ran and kind != "doc":
    expected = []
    if kind == "lib":
        root = os.path.join(pkg, "src")
        for d, _, files in os.walk(root):
            for f in sorted(files):
                if f.endswith(".rs"):
                    p = os.path.join(d, f)
                    expected += scan(p, module_of(root, p))
    else:
        p = os.path.join(pkg, "tests", target[len("test/"):] + ".rs")
        if os.path.exists(p):
            expected = scan(p, [])
    for name in expected:
        results.setdefault(name, "FAILED")

for name in sorted(results):
    print("MSWB_RESULT {} {}{}".format(results[name], prefix, name))
"""

_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    passed_tests: set = set()
    failed_tests: set = set()
    skipped_tests: set = set()
    for line in test_log.splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.group(1), m.group(2)
        if status == "FAILED":
            failed_tests.add(name)
        elif status == "PASSED":
            passed_tests.add(name)
        else:
            skipped_tests.add(name)
    passed_tests -= failed_tests
    skipped_tests -= failed_tests | passed_tests
    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class ImageBase(Image):
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
        return _RUST_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_RUST_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            f"{_APT_RUN}\n"
            "WORKDIR /home/\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}\n\n'
            'CMD ["/bin/bash"]\n'
        )


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        pkg_flags = " ".join(f"-p {p}" for p in _PACKAGES.split())
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, pkg_flags=pkg_flags)),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo, packages=_PACKAGES)),
            File(".", "mswb_targets.sh", _TARGETS_SH),
            File(".", "mswb_vendor.py", _VENDOR_PY),
            File(".", "mswb_digest.py", _DIGEST_PY),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{Image._HARDENING_BLOCK.rstrip(chr(10))}\n\n"
        )


@Instance.register("linebender", "druid")
@Instance.register("linebender", _INTERVAL_NAME)
class DRUID_818_TO_765(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

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
