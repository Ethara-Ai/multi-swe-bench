import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.java.bazelbuild.bazel import Bazel as _BazelFallback

_BASE_IMAGE = "buildpack-deps:jammy"
_INTERVAL_NAME = "bazel_28801_to_24443"
_BASE_TAG = "base-28801_to_24443"
_JDK = "21"
_BAZELISK_VERSION = "v1.25.0"

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
    LC_ALL=C.UTF-8 \\
    JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk
""".format(jdk=_JDK)

_TOOL_CHECK = "RUN git --version && curl --version\n"

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

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates git curl build-essential python3 zip unzip file openjdk-{jdk}-jdk
rm -rf /var/lib/apt/lists/*
ln -sfn "/usr/lib/jvm/java-{jdk}-openjdk-$(dpkg --print-architecture)" /usr/lib/jvm/java-{jdk}-openjdk
curl -fsSL --retry 5 -o /usr/local/bin/bazelisk "https://github.com/bazelbuild/bazelisk/releases/download/{bazelisk}/bazelisk-linux-$(dpkg --print-architecture)"
chmod +x /usr/local/bin/bazelisk
id -u mswb > /dev/null 2>&1 || useradd -m -d /home/mswb -s /bin/bash mswb
git config --global --add safe.directory '*'
chown -R mswb:mswb /home/{repo}
cat > /usr/local/bin/bazel <<'EOF'
#!/bin/bash
exec runuser -u mswb -- env HOME=/home/mswb USER=mswb /usr/local/bin/bazelisk "$@"
EOF
chmod +x /usr/local/bin/bazel
cat > /home/mswb/.bazelrc <<'EOF'
startup --host_jvm_args=-Xmx4g
common --lockfile_mode=off
build --symlink_prefix=/
build --jobs=4
build --local_resources=memory=HOST_RAM*.5
build --color=no
build --curses=no
EOF
chown mswb:mswb /home/mswb/.bazelrc
runuser -u mswb -- git config --global --add safe.directory '*'
for attempt in 1 2 3 4 5; do
    if bazel version; then break; fi
    sleep 20
done
python3 /home/mswb_bazel.py targets /home/test.patch > /tmp/mswb_warm_targets.txt
cat /tmp/mswb_warm_targets.txt
if [ -s /tmp/mswb_warm_targets.txt ]; then
    mapfile -t WARM < /tmp/mswb_warm_targets.txt
    if ! bazel build --keep_going "${{WARM[@]}}"; then
        echo "MSWB_WARM_INCOMPLETE"
    fi
fi
git checkout -- .

test -x /usr/local/bin/bazel && java -version 2>&1 | grep -q '"{jdk}' && bazel version | grep -q 'Build label' && test -d "$(bazel info output_base)/external" && echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
set -o pipefail
export CI=true
cd /home/{repo}
python3 /home/mswb_bazel.py targets /home/test.patch > /tmp/mswb_targets.txt
cat /tmp/mswb_targets.txt
if [ ! -s /tmp/mswb_targets.txt ]; then
    echo "MSWB_NO_TARGETS"
    exit 0
fi
mapfile -t TARGETS < /tmp/mswb_targets.txt
bazel test "${{TARGETS[@]}}" --build_tests_only --keep_going --nocache_test_results --test_output=errors --test_timeout=900 > /tmp/mswb_bazel.log 2>&1
echo "MSWB_BAZEL_EXIT=$?"
tail -n 300 /tmp/mswb_bazel.log
TESTLOGS=$(bazel info bazel-testlogs 2>/dev/null)
python3 /home/mswb_bazel.py digest /home/test.patch "$TESTLOGS"
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

_BAZEL_PY = r'''import os
import re
import sys
import xml.etree.ElementTree as ET

RULE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\(', re.M)
NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')
TEST_RE = re.compile(r'@Test\b[^;{]*?\b(?:public\s+)?void\s+([A-Za-z0-9_]+)\s*\(', re.S)
PKG_RE = re.compile(r'^\s*package\s+([A-Za-z0-9_.]+)\s*;', re.M)


def patch_paths(patch):
    out = []
    with open(patch, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("+++ b/"):
                p = line[6:].strip()
                if p not in out:
                    out.append(p)
    return sorted(out)


def is_test_file(p):
    stem, ext = os.path.splitext(os.path.basename(p))
    if ext == ".java":
        return "src/test/" in p and (stem.endswith("Test") or stem.endswith("Tests"))
    if ext in (".py", ".sh"):
        return p.startswith("src/test/") and stem.endswith("_test")
    return False


def find_build(path):
    d = os.path.dirname(path)
    while d and d != "src":
        for n in ("BUILD", "BUILD.bazel"):
            if os.path.isfile(os.path.join(d, n)):
                return d, os.path.join(d, n)
        d = os.path.dirname(d)
    return None, None


def all_rules(build):
    with open(build, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    starts = [(m.start(), m.group(1)) for m in RULE_RE.finditer(text)]
    rules = []
    for i, (pos, kind) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        block = text[pos:end]
        m = NAME_RE.search(block)
        if m:
            rules.append((kind, m.group(1), block))
    return rules


def test_rules(build):
    return [(n, b) for k, n, b in all_rules(build) if k.endswith("_test")]


def via_library(build, rel, same_dir):
    rules = all_rules(build)
    libs = []
    for k, n, b in rules:
        if k.endswith("_test") or "srcs" not in b:
            continue
        if '"' + rel + '"' in b or (same_dir and "glob(" in b):
            libs.append(n)
    hit = []
    for lib in libs:
        for k, n, b in rules:
            if k.endswith("_test") and ('":' + lib + '"' in b or '"' + lib + '"' in b):
                if n not in hit:
                    hit.append(n)
    return hit


def mapping(patch):
    result = {}
    for p in patch_paths(patch):
        if not os.path.isfile(p) or not is_test_file(p):
            continue
        d, build = find_build(p)
        if not build:
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        rel = os.path.relpath(p, d)
        rules = test_rules(build)
        hit = [n for n, _ in rules if n == stem]
        if not hit:
            hit = [n for n, b in rules if '"' + rel + '"' in b]
        same_dir = os.path.dirname(p) == d
        if not hit and p.endswith(".java") and same_dir:
            hit = [n for n, b in rules if "glob(" in b]
        if not hit:
            hit = via_library(build, rel, same_dir)
        for n in hit:
            result.setdefault("//" + d + ":" + n, []).append(p)
    return result


def enumerate_java(files):
    ids = []
    for f in files:
        if not f.endswith(".java"):
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        m = PKG_RE.search(src)
        cls = os.path.splitext(os.path.basename(f))[0]
        full = (m.group(1) + "." + cls) if m else cls
        for t in TEST_RE.findall(src):
            ids.append(full + "." + t)
    return ids


def digest(patch, testlogs):
    order = {"FAILED": 3, "PASSED": 2, "SKIPPED": 1}
    for label, files in sorted(mapping(patch).items()):
        pkg, name = label[2:].split(":", 1)
        root = os.path.join(testlogs, pkg, name) if testlogs else ""
        results = {}
        if root and os.path.isdir(root):
            for dirpath, _, names in os.walk(root):
                if "test.xml" not in names:
                    continue
                try:
                    tree = ET.parse(os.path.join(dirpath, "test.xml"))
                except ET.ParseError:
                    continue
                for tc in tree.iter("testcase"):
                    nm = (tc.get("name") or "").strip()
                    cls = (tc.get("classname") or "").strip()
                    if not nm:
                        continue
                    tid = "bazel::" + label + "::" + (cls + "." + nm if cls else nm)
                    if tc.find("failure") is not None or tc.find("error") is not None:
                        st = "FAILED"
                    elif tc.find("skipped") is not None:
                        st = "SKIPPED"
                    else:
                        st = "PASSED"
                    if order[st] > order.get(results.get(tid), 0):
                        results[tid] = st
        if not results:
            names = enumerate_java(files)
            if names:
                for n in names:
                    results["bazel::" + label + "::" + n] = "FAILED"
            else:
                results["bazel::" + label] = "FAILED"
        for tid in sorted(results):
            print("MSWB_RESULT " + results[tid] + " " + tid)


if __name__ == "__main__":
    if sys.argv[1] == "targets":
        for label in sorted(mapping(sys.argv[2])):
            print(label)
    else:
        digest(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
'''

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
        return _BASE_IMAGE

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
            f"FROM {_BASE_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            f"{_TOOL_CHECK}\n"
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
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, jdk=_JDK, bazelisk=_BAZELISK_VERSION)),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo)),
            File(".", "mswb_bazel.py", _BAZEL_PY),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return (
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copies}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            + Image._HARDENING_BLOCK.rstrip(chr(10))
            + "\n\n"
        )


@Instance.register("bazelbuild", _INTERVAL_NAME)
class BAZEL_28801_TO_24443(Instance):
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
        return run_cmd if run_cmd else "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd if test_patch_run_cmd else "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd if fix_patch_run_cmd else "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        return _parse_mswb_log(test_log)


_INTERVAL_RE = re.compile(r"^bazel_(?P<hi>\d+)_to_(?P<lo>\d+)$")
_INTERVAL_MATCH = _INTERVAL_RE.match(_INTERVAL_NAME)
_INTERVAL_LO = int(_INTERVAL_MATCH.group("lo"))
_INTERVAL_HI = int(_INTERVAL_MATCH.group("hi"))


@Instance.register("bazelbuild", "bazel")
class BAZEL_ROUTER(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _INTERVAL_LO <= pr.number <= _INTERVAL_HI:
            return BAZEL_28801_TO_24443(pr, config, *args, **kwargs)
        return _BazelFallback(pr, config, *args, **kwargs)
