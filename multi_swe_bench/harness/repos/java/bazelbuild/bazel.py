import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

DATASET_PR_MIN = 15844
DATASET_PR_MAX = 16680
BASE_TAG = f"base-pr-{DATASET_PR_MIN}-{DATASET_PR_MAX}"
BAZELISK_VERSION = "v1.25.0"
DEFAULT_TEST_TARGETS = "//src/test/..."
BUILD_FILES = ("BUILD", "BUILD.bazel")

BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM {base_image}

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    && rm -rf /var/lib/apt/lists/*

{fetch}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""

PR_DOCKERFILE = """FROM {base}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 1; \\
    git config --local pack.windowMemory 32m; \\
    git config --local pack.packSizeLimit 128m; \\
    git config --local pack.deltaCacheSize 32m; \\
    git gc --prune=now; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
            git config --local pack.threads 1; \\
            git config --local pack.windowMemory 32m; \\
            git config --local pack.packSizeLimit 128m; \\
            git config --local pack.deltaCacheSize 32m; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""

BEP_DIGEST = """python3 - <<'PYEOF'
import json
status = {}
try:
    fh = open('/tmp/bep.json')
except OSError:
    fh = []
for line in fh:
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    ident = ev.get('id') or {}
    tc = ident.get('targetCompleted')
    if tc and 'completed' in ev:
        ok = (ev.get('completed') or {}).get('success', False)
        status.setdefault(tc.get('label', ''), 'BUILT' if ok else 'FAILED TO BUILD')
    ts = ident.get('testSummary')
    if ts:
        status[ts.get('label', '')] = (ev.get('testSummary') or {}).get('overallStatus', 'NO_STATUS')
print('===BEP BEGIN===')
for label in sorted(status):
    if label:
        print(status[label], label)
print('===BEP END===')
PYEOF"""

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

PREPARE_SH = """#!/bin/bash
set -e

apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    git \\
    curl \\
    build-essential \\
    python3 \\
    zip \\
    unzip \\
    file \\
    openjdk-{jdk}-jdk \\
    && rm -rf /var/lib/apt/lists/*

ln -sf /usr/lib/jvm/java-{jdk}-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-{jdk}-openjdk

ARCH=$(dpkg --print-architecture)
curl -fSsL -o /usr/local/bin/bazel https://github.com/bazelbuild/bazelisk/releases/download/{bazelisk}/bazelisk-linux-${{ARCH}}
chmod +x /usr/local/bin/bazel

export JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk
export LC_ALL=C.UTF-8
export CI=true

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

if [ ! -f .bazelversion ]; then
  echo "{bazel_version}" > .bazelversion
fi

bazel version || true
bazel build {targets} --noshow_progress 2>&1 || true
"""

RUN_SH = """#!/bin/bash
set -eo pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk}-openjdk
export LC_ALL=C.UTF-8
export CI=true

cd /home/{repo}
{patch}{test_cmd}
"""

PATCH_LINES = {
    "run.sh": "",
    "test-run.sh": "git apply --whitespace=nowarn /home/test.patch\n",
    "fix-run.sh": "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n",
}


def _jdk_for_ref(ref: str) -> str:
    m = re.match(r"release-(\d+)\.", ref)
    if not m:
        return "21"
    major = int(m.group(1))
    return "21" if major >= 8 else "17" if major >= 7 else "11"


def _bazel_version_for_ref(ref: str) -> str:
    m = re.match(r"release-(\d+)\.(\d+)(?:\.(\d+))?", ref) or re.search(
        r"(\d+)\.(\d+)\.(\d+)", ref
    )
    if not m:
        return "last_green"
    return f"{m.group(1)}.{m.group(2)}.{m.group(3) or '0'}"


class _BazelImage(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config


class BazelImageBase(_BazelImage):
    def dependency(self) -> str | Image:
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            base_image = base_image.image_full_name()
        repo = self.pr.repo
        fetch = (
            f'RUN git clone "${{REPO_URL}}" /home/{repo}'
            if self.config.need_clone
            else f"COPY {repo} /home/{repo}"
        )
        return BASE_DOCKERFILE.format(
            base_image=base_image,
            org=self.pr.org,
            repo=repo,
            jdk=_jdk_for_ref(self.pr.base.ref),
            bazelisk=BAZELISK_VERSION,
            fetch=fetch,
        )


class BazelImageDefault(_BazelImage):
    def dependency(self) -> Image | None:
        return BazelImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    @staticmethod
    def _diff_paths(*patches: str) -> list[str]:
        return [
            m.group(2)
            for patch in patches
            for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch)
        ]

    @classmethod
    def _find_build_dirs(cls, *patches: str) -> set[str]:
        return {
            p.rsplit("/", 1)[0] if "/" in p else ""
            for p in cls._diff_paths(*patches)
            if (p.rsplit("/", 1)[-1] if "/" in p else p) in BUILD_FILES
        }

    @staticmethod
    def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set[str]) -> bool:
        return bool(parent_dir) and (
            "/test/py/" in pkg_dir
            or "/testdata/" in pkg_dir
            or pkg_dir.endswith(("/testdata", "/bin"))
            or parent_dir in build_dirs
        )

    def _extract_test_targets(self) -> str:
        build_dirs = self._find_build_dirs(self.pr.test_patch, self.pr.fix_patch)
        test_files = [
            p
            for p in self._diff_paths(self.pr.test_patch)
            if ("/test/" in p or "/javatests/" in p)
            and not p.startswith("src/main/")
            and (p.rsplit("/", 1)[-1] if "/" in p else p) not in BUILD_FILES
        ]
        if not test_files:
            return DEFAULT_TEST_TARGETS

        targets: set[str] = set()
        for path in test_files:
            pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            basename = path.rsplit("/", 1)[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename
            if pkg_dir in build_dirs:
                targets.add(f"//{pkg_dir}/...")
                continue

            parent, found = pkg_dir, False
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                if parent in build_dirs:
                    targets.add(f"//{parent}:{stem}")
                    found = True
                    break
            if found:
                continue

            parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
            if self._likely_subdir(pkg_dir, parent_dir, build_dirs):
                targets.add(f"//{parent_dir}:{stem}")
            elif basename.endswith(".sh"):
                targets.add(f"//{pkg_dir}:{stem}")
            else:
                targets.add(f"//{pkg_dir}/...")
        return " ".join(sorted(targets)) if targets else DEFAULT_TEST_TARGETS

    def files(self) -> list[File]:
        jdk = _jdk_for_ref(self.pr.base.ref)
        targets = self._extract_test_targets()
        repo = self.pr.repo
        test_cmd = (
            "rm -f /tmp/bep.json\n"
            "bazel_rc=0\n"
            f"bazel test {targets}"
            " --build_tests_only --test_output=errors --test_tag_filters=-manual"
            " --test_timeout=600 --keep_going --jobs=6"
            " --build_event_json_file=/tmp/bep.json 2>&1 || bazel_rc=$?\n"
            'echo "bazel exit code: $bazel_rc"\n'
            f"{BEP_DIGEST}"
        )
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                PREPARE_SH.format(
                    jdk=jdk,
                    repo=repo,
                    sha=self.pr.base.sha,
                    bazel_version=_bazel_version_for_ref(self.pr.base.ref),
                    targets=targets,
                    bazelisk=BAZELISK_VERSION,
                ),
            ),
        ] + [
            File(".", name, RUN_SH.format(jdk=jdk, repo=repo, patch=patch, test_cmd=test_cmd))
            for name, patch in PATCH_LINES.items()
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return PR_DOCKERFILE.format(
            base=f"{image.image_name()}:{image.image_tag()}",
            sha=self.pr.base.sha,
            copy_commands=copy_commands,
        )


@Instance.register("bazelbuild", "bazel")
class Bazel(Instance):
    BEP_STATUSES = (
        "FAILED TO BUILD|NO_STATUS|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|INCOMPLETE|BUILT"
    )
    CONSOLE_STATUSES = "FAILED TO BUILD|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|INCOMPLETE"
    PASS_STATUSES = ("PASSED", "FLAKY")
    FAIL_STATUSES = ("FAILED", "TIMEOUT", "INCOMPLETE", "FAILED TO BUILD")

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return BazelImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def _parse_bep(self, log: str) -> dict[str, str]:
        block = re.search(r"===BEP BEGIN===\n(.*?)===BEP END===", log, re.DOTALL)
        if not block:
            return {}
        pattern = re.compile(rf"^({self.BEP_STATUSES})\s+(//\S+)$")
        status: dict[str, str] = {}
        for line in block.group(1).splitlines():
            m = pattern.match(line.strip())
            if m and m.group(1) != "BUILT":
                status[m.group(2)] = m.group(1).replace("NO_STATUS", "NO STATUS")
        return status

    def _parse_console(self, log: str) -> dict[str, str]:
        pattern = re.compile(
            r"^(//\S+)\s+(?:\(cached\)\s+)?"
            rf"({self.CONSOLE_STATUSES})"
            r"(?:,\s+passed\s+\d+/\d+)?"
            r"(?:\s+in\s+\d+\s+out\s+of\s+\d+)?"
            r"(?:\s+in\s+[\d.]+s)?\s*$",
            re.MULTILINE,
        )
        status: dict[str, str] = {}
        for m in pattern.finditer(log):
            target = m.group(1)
            if target.startswith("//src/") and status.get(target, "PASSED") == "PASSED":
                status[target] = m.group(2)
        return status

    @staticmethod
    def _parse_junit(log: str) -> tuple[set[str], set[str], set[str]]:
        passed, failed, skipped = set(), set(), set()
        pattern = re.compile(
            r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
            r"\s*Skipped:\s*(\d+),\s*Time elapsed:\s*[\d.]+\s*s(?:ec)?"
            r".*?(?:in\s+(\S+)|$)",
            re.MULTILINE,
        )
        for m in pattern.finditer(log):
            run, failures, errors, skips = (int(m.group(i)) for i in range(1, 5))
            name = m.group(5) or f"test_suite_{m.start()}"
            if failures or errors:
                failed.add(name)
            elif skips == run:
                skipped.add(name)
            elif run > 0:
                passed.add(name)
        return passed, failed, skipped

    def parse_log(self, test_log: str) -> TestResult:
        test_log = re.sub(r"\x1B\[[0-?9;]*[mK]", "", test_log)
        status = self._parse_bep(test_log) or self._parse_console(test_log)
        passed = {t for t, s in status.items() if s in self.PASS_STATUSES}
        failed = {t for t, s in status.items() if s in self.FAIL_STATUSES}
        skipped = {t for t, s in status.items() if s == "NO STATUS"}
        if not passed and not failed:
            passed, failed, skipped = self._parse_junit(test_log)
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
