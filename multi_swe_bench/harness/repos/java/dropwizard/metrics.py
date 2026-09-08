import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PR_NUMBERS: set = set()

SHELL_ENV = """\
export LC_ALL=C.UTF-8
export MAVEN_OPTS=-Djava.awt.headless=true"""

# Reads target/surefire-reports/TEST-*.xml and emits one line per test METHOD
# (passing ones too, which the Maven console never names). Same emitter the
# in-repo dazzleconf Maven config uses.
SUREFIRE_REPORT_PY = r"""#!/usr/bin/env python3
import glob
import os
import sys
import xml.etree.ElementTree as ET


def status_of(testcase):
    for child in testcase:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in ("failure", "error"):
            return "FAIL"
        if tag == "skipped":
            return "SKIP"
    return "PASS"


def source_path(root, xml_path, classname):
    module_dir = os.path.dirname(os.path.dirname(os.path.dirname(xml_path)))
    top_level = classname.split("$", 1)[0]
    relative = top_level.replace(".", os.sep) + ".java"
    candidate = os.path.join(module_dir, "src", "test", "java", relative)
    if os.path.isfile(candidate):
        return os.path.relpath(candidate, root)
    basename = top_level.rsplit(".", 1)[-1] + ".java"
    for dirpath, _dirnames, filenames in os.walk(os.path.join(module_dir, "src")):
        if basename in filenames:
            return os.path.relpath(os.path.join(dirpath, basename), root)
    return None


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    pattern = os.path.join(root, "**", "target", "surefire-reports", "TEST-*.xml")
    for xml_path in sorted(glob.glob(pattern, recursive=True)):
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue
        for testcase in tree.getroot().iter("testcase"):
            classname = testcase.get("classname") or ""
            name = testcase.get("name") or ""
            if not classname or not name:
                continue
            path = source_path(root, xml_path, classname)
            if path:
                test_id = "{0}::{1}".format(path, name)
            else:
                test_id = "{0}#{1}".format(classname, name)
            print("SUREFIRE_TESTCASE {0} {1}".format(status_of(testcase), test_id))


if __name__ == "__main__":
    main()
"""


def _target_module(test_patch: str) -> str:
    """The Maven module whose tests this PR touches, read off the test patch.
    A test file lives at <module>/src/test/java/...; everything before '/src/' is
    the module directory. Derived from the patch, never written down."""
    for path in re.findall(r"diff --git a/(\S+) b/\S+", test_patch or ""):
        if "/src/" in path:
            top = path.split("/src/")[0]
            if top:
                return top
    return ""


def _mvn_test(module: str) -> str:
    scope = f"-pl {module} -am " if module else ""
    return (
        "mvn -B -ntp " + scope + "clean test "
        "-Dstyle.color=never -Dmaven.test.failure.ignore=true -fae"
    )


class MetricsImageBase(Image):
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
        return "maven:3.9-eclipse-temurin-17"

    def image_tag(self) -> str:
        nums = _PR_NUMBERS or {self.pr.number}
        return f"base-{min(nums)}-{max(nums)}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

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
    SSL_CERT_FILE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}"

WORKDIR /home/

RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends git python3 ca-certificates; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}
CMD ["/bin/bash"]
"""


class MetricsImageDefault(Image):
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
        return MetricsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        repo = self.pr.repo
        sha = self.pr.base.sha
        module = _target_module(self.pr.test_patch)
        mvn = _mvn_test(module)

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"; exit 1\n'
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"; git status --porcelain; exit 1\n'
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"{SHELL_ENV}\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "warm() {\n"
            f"  if timeout 2400 {mvn} > /tmp/warm.log 2>&1; then\n"
            '    echo "warm-up $1: OK" >> /home/.warm_status\n'
            "  else\n"
            '    echo "warm-up $1: INCOMPLETE (exit $?)" >> /home/.warm_status\n'
            "    tail -20 /tmp/warm.log || true\n"
            "  fi\n"
            "}\n"
            "warm base\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true\n"
            "warm fix\n"
            "cat /home/.warm_status || true\n"
            "git reset --hard\n"
            "git clean -fdx\n"
            "bash /home/check_git_changes.sh\n"
        )

        def _stage(apply_line: str) -> str:
            body = [
                "#!/bin/bash",
                "set -eo pipefail",
                SHELL_ENV,
                f"cd /home/{repo}",
                "find . -type d -name surefire-reports -prune -exec rm -rf {} + 2>/dev/null || true",
                "git reset --hard",
                "git clean -fdx",
            ]
            if apply_line:
                body.append(apply_line)
            body.append(f"{mvn}")
            body.append('echo "----- surefire per-test results -----"')
            body.append("python3 /home/surefire_report.py .")
            return "\n".join(body) + "\n"

        run_sh = _stage("")
        test_run_sh = _stage("git apply --whitespace=nowarn /home/test.patch")
        fix_run_sh = _stage("git apply --whitespace=nowarn /home/test.patch /home/fix.patch")

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "surefire_report.py", SUREFIRE_REPORT_PY),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
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


@Instance.register("dropwizard", "metrics")
class Metrics(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _PR_NUMBERS.add(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MetricsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set = set()
        failed_tests: set = set()
        skipped_tests: set = set()

        clean = re.sub(r"\x1b\[[0-9;]*m", "", log)
        for line in clean.splitlines():
            m = re.match(r"\s*SUREFIRE_TESTCASE\s+(PASS|FAIL|SKIP)\s+(\S+)", line)
            if not m:
                continue
            status, test_id = m.group(1), m.group(2)
            if status == "PASS":
                passed_tests.add(test_id)
            elif status == "FAIL":
                failed_tests.add(test_id)
            else:
                skipped_tests.add(test_id)

        passed_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
