import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "python:3.8-bullseye"

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

_RUN_TESTS_PY = '''import sys
import unittest

MARKER = "MPF_TEST_RESULT:"


class EmittingTestResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.collected = []

    def _emit(self, status, test):
        self.collected.append((status, test.id()))
        sys.stdout.write("\\n{} {} {}\\n".format(MARKER, status, test.id()))
        sys.stdout.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        self._emit("PASS", test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._emit("FAIL", test)

    def addError(self, test, err):
        super().addError(test, err)
        self._emit("FAIL", test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._emit("SKIP", test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._emit("PASS", test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._emit("FAIL", test)


def main():
    suite = unittest.TestLoader().discover(start_dir="mpf/tests", top_level_dir=".")
    runner = unittest.TextTestRunner(
        stream=sys.stdout, verbosity=1, resultclass=EmittingTestResult
    )
    result = runner.run(suite)
    sys.stdout.write("\\nMPF_TEST_SUMMARY_BEGIN\\n")
    for status, name in getattr(result, "collected", []):
        sys.stdout.write("{} {} {}\\n".format(MARKER, status, name))
    sys.stdout.write("MPF_TEST_SUMMARY_END\\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_PREPARE_SH = """#!/bin/bash
set -euo pipefail

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {sha}
bash /home/check_git_changes.sh

python -m pip install --no-cache-dir --upgrade "pip<24" "setuptools<60" "wheel<0.42"
python -m pip install --no-cache-dir -e .

python -c "import mpf, mpf._version; from mpf.tests.MpfTestCase import MpfTestCase; print(mpf._version.__version__); print('DEPS_OK')"
"""

_APPLY_STEP = """if ! git apply --whitespace=nowarn {patches}; then
    echo "Error: git apply {patches} failed" >&2
    exit 1
fi

"""

_RUN_SH = """#!/bin/bash
set -eo pipefail

export CI=true
export PYTHONUNBUFFERED=1

cd /home/{repo}

{apply_step}python /home/run_tests.py
"""


class MpfImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

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
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    PYTHONUNBUFFERED=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
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

RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends --no-upgrade \\
        git \\
        ca-certificates \\
        build-essential; \\
    rm -rf /var/lib/apt/lists/*; \\
    command -v git; \\
    command -v gcc; \\
    command -v make; \\
    test -f "${{CA_CERT_PATH}}"

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

{self.clear_env}

CMD ["/bin/bash"]
"""


class MpfImageDefault(Image):
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
        return MpfImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _run_script(self, name: str, *patches: str) -> File:
        apply_step = (
            _APPLY_STEP.format(patches=" ".join(patches)) if patches else ""
        )
        return File(
            ".",
            name,
            _RUN_SH.format(repo=self.pr.repo, apply_step=apply_step),
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "run_tests.py", _RUN_TESTS_PY),
            File(
                ".",
                "prepare.sh",
                _PREPARE_SH.format(repo=self.pr.repo, sha=self.pr.base.sha),
            ),
            self._run_script("run.sh"),
            self._run_script("test-run.sh", "/home/test.patch"),
            self._run_script("fix-run.sh", "/home/test.patch", "/home/fix.patch"),
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

{copy_commands}

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{hardening}

{self.clear_env}
"""


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")

_RESULT_RE = re.compile(r"MPF_TEST_RESULT:\s+(PASS|FAIL|SKIP)\s+(\S+)")


@Instance.register("missionpinball", "mpf")
class Mpf(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MpfImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for line in _ANSI_RE.sub("", test_log).replace("\r", "").split("\n"):
            match = _RESULT_RE.search(line)
            if not match:
                continue

            status, name = match.group(1), match.group(2)
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
