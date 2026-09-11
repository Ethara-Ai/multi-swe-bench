import re
import textwrap
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest



class Bazel16733To20260ImageBase(Image):
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
        return "ubuntu:22.04"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def _get_jdk_version(self) -> str:
        ref = self.pr.base.ref
        if ref == "master":
            return "21"

        m = re.match(r"release-(\d+)\.", ref)
        if m:
            major = int(m.group(1))
            if major >= 8:
                return "21"
            elif major >= 7:
                return "17"
            else:
                return "11"

        return "21"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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

{self.global_env}

WORKDIR /home/
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class Bazel16733To20260ImageDefault(Image):

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
        return Bazel16733To20260ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _get_bazel_version_for_ref(self) -> str:
        ref = self.pr.base.ref
        m = re.match(r"release-(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        m = re.match(r"release-(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.0"

        m = re.search(r"(\d+)\.(\d+)\.(\d+)", ref)
        if m:
            return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

        return "last_green"

    @staticmethod
    def _find_build_dirs(*patches: str) -> set[str]:
        dirs: set[str] = set()
        for patch in patches:
            for m in re.finditer(r"diff --git a/(.+?) b/(.+)", patch):
                path = m.group(2)
                basename = path.rsplit("/", 1)[-1] if "/" in path else path
                if basename in ("BUILD", "BUILD.bazel"):
                    pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
                    dirs.add(pkg_dir)
        return dirs

    @staticmethod
    def _likely_subdir(pkg_dir: str, parent_dir: str, build_dirs: set[str]) -> bool:
        if not parent_dir:
            return False
        if "/test/py/" in pkg_dir:
            return True
        if "/testdata/" in pkg_dir:
            return True
        if pkg_dir.endswith("/testdata"):
            return True
        if pkg_dir.endswith("/bin"):
            return True
        if parent_dir in build_dirs:
            return True
        return False

    def _extract_test_targets(self) -> str:
        all_build_dirs = self._find_build_dirs(self.pr.test_patch, self.pr.fix_patch)

        test_files: list[str] = []
        for m in re.finditer(r"diff --git a/(.+?) b/(.+)", self.pr.test_patch):
            path = m.group(2)
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if "/test/" not in path and "/javatests/" not in path:
                continue
            if path.startswith("src/main/"):
                continue
            if basename in ("BUILD", "BUILD.bazel"):
                continue
            test_files.append(path)

        if not test_files:
            return "//src/test/..."

        targets: set[str] = set()

        for path in test_files:
            pkg_dir = path.rsplit("/", 1)[0] if "/" in path else ""
            basename = path.rsplit("/", 1)[-1]
            stem = basename.rsplit(".", 1)[0] if "." in basename else basename

            if pkg_dir in all_build_dirs:
                targets.add(f"//{pkg_dir}/...")
                continue

            parent = pkg_dir
            found_parent_pkg = False
            while "/" in parent:
                parent = parent.rsplit("/", 1)[0]
                if parent in all_build_dirs:
                    targets.add(f"//{parent}:{stem}")
                    found_parent_pkg = True
                    break

            if found_parent_pkg:
                continue

            parent_dir = pkg_dir.rsplit("/", 1)[0] if "/" in pkg_dir else ""
            if self._likely_subdir(pkg_dir, parent_dir, all_build_dirs):
                targets.add(f"//{parent_dir}:{stem}")
            elif basename.endswith(".sh"):
                targets.add(f"//{pkg_dir}:{stem}")
            else:
                targets.add(f"//{pkg_dir}/...")

        if not targets:
            return "//src/test/..."

        return " ".join(sorted(targets))

    def files(self) -> list[File]:
        jdk_version = Bazel16733To20260ImageBase(self.pr, self._config)._get_jdk_version()
        test_targets = self._extract_test_targets()
        bazel_version_pin = self._get_bazel_version_for_ref()

        bep_digest = """python3 - <<'PYEOF'
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

        test_cmd = (
            "rm -f /tmp/bep.json\n"
            f"bazel test {test_targets}"
            " --build_tests_only --test_output=errors --test_tag_filters=-manual"
            " --test_timeout=600 --keep_going --jobs=6"
            " --javacopt=-Xep:ComparisonOutOfRange:OFF"
            " --build_event_json_file=/tmp/bep.json 2>&1\n"
            f"{bep_digest}"
        )

        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

apt-get update && apt-get install -y \\
    git ca-certificates curl openjdk-{jdk_version}-jdk build-essential zip unzip python3 file \\
    && rm -rf /var/lib/apt/lists/*

ln -sf /usr/lib/jvm/java-{jdk_version}-openjdk-$(dpkg --print-architecture) /usr/lib/jvm/java-{jdk_version}-openjdk

ARCH=$(dpkg --print-architecture)
curl -fSsL -o /usr/local/bin/bazel https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-${{ARCH}}
chmod +x /usr/local/bin/bazel

groupadd -r bazeluser && useradd -r -g bazeluser -m -d /home/bazeluser bazeluser
chown -R bazeluser:bazeluser /home/

git config --global --add safe.directory /home/{pr.repo}

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

if [ ! -f .bazelversion ]; then
  echo "{bazel_version}" > .bazelversion
fi

su bazeluser -c "cd /home/{pr.repo} && export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk && export LC_ALL=C.UTF-8 && bazel version" || true
timeout 900 su bazeluser -c "cd /home/{pr.repo} && export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk && export LC_ALL=C.UTF-8 && bazel build {test_targets} --noshow_progress --javacopt=-Xep:ComparisonOutOfRange:OFF 2>&1" || true
""".format(pr=self.pr, bazel_version=bazel_version_pin, test_targets=test_targets, jdk_version=jdk_version),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -o pipefail

export JAVA_HOME=/usr/lib/jvm/java-{jdk_version}-openjdk
export LC_ALL=C.UTF-8

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
exit 0
""".format(pr=self.pr, test_cmd=test_cmd, jdk_version=jdk_version),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"
        copy_commands = copy_commands.rstrip("\n")

        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}

RUN bash /home/prepare.sh

USER bazeluser

{hardening}

{self.clear_env}
"""


@Instance.register("bazelbuild", "16733_to_20260")
class Bazel16733To20260(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]: # type: ignore
        return Bazel16733To20260ImageDefault(self.pr, self._config)

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
        target_status: dict[str, str] = {}

        bep_block = re.search(
            r"===BEP BEGIN===\n(.*?)===BEP END===", test_log, re.DOTALL
        )
        if bep_block:
            re_bep = re.compile(
                r"^(FAILED TO BUILD|NO_STATUS|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|"
                r"INCOMPLETE|BUILT)\s+(//\S+)$"
            )
            for line in bep_block.group(1).splitlines():
                m = re_bep.match(line.strip())
                if not m:
                    continue
                status, target = m.group(1), m.group(2)
                if status == "BUILT":
                    continue
                target_status[target] = status.replace("NO_STATUS", "NO STATUS")

        re_test_result = re.compile(
            r"^(//\S+)\s+"
            r"(?:\(cached\)\s+)?"
            r"(FAILED TO BUILD|NO STATUS|PASSED|FAILED|TIMEOUT|FLAKY|INCOMPLETE)"
            r"(?:,\s+passed\s+\d+/\d+)?"
            r"(?:\s+in\s+\d+\s+out\s+of\s+\d+)?"
            r"(?:\s+in\s+[\d.]+s)?\s*$",
            re.MULTILINE,
        )

        for match in (
            re_test_result.finditer(test_log) if not target_status else ()
        ):
            target = match.group(1)

            if not target.startswith("//src/"):
                continue

            status = match.group(2)

            if target_status.get(target, "PASSED") != "PASSED":
                continue
            target_status[target] = status

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        for target, status in target_status.items():
            if status in ("PASSED", "FLAKY"):
                passed_tests.add(target)
            elif status in ("FAILED", "TIMEOUT", "INCOMPLETE", "FAILED TO BUILD"):
                failed_tests.add(target)
            elif status == "NO STATUS":
                skipped_tests.add(target)

        if not passed_tests and not failed_tests:
            re_junit = re.compile(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
                r"\s*Skipped:\s*(\d+),\s*Time elapsed:\s*[\d.]+\s*s(?:ec)?"
                r".*?(?:in\s+(\S+)|$)",
                re.MULTILINE,
            )
            for match in re_junit.finditer(test_log):
                tests_run = int(match.group(1))
                failures = int(match.group(2))
                errors = int(match.group(3))
                skipped = int(match.group(4))
                test_name = match.group(5) if match.group(5) else f"test_suite_{match.start()}"

                if tests_run > 0 and failures == 0 and errors == 0 and skipped != tests_run:
                    passed_tests.add(test_name)
                elif failures > 0 or errors > 0:
                    failed_tests.add(test_name)
                elif skipped == tests_run:
                    skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
