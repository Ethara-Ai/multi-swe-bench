import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase4682To4671(Image):
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
        return "python:3.12-slim"

    def image_tag(self) -> str:
        return "base-4682_to_4671"

    def workdir(self) -> str:
        return "base-4682_to_4671"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return """# syntax=docker/dockerfile:1.6

FROM python:3.12-slim

ARG TARGETARCH
ARG REPO_URL="https://github.com/python-telegram-bot/python-telegram-bot.git"
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="python-telegram-bot/python-telegram-bot" \\
      org.opencontainers.image.description="python-telegram-bot/python-telegram-bot 4682_to_4671 Docker image" \\
      org.opencontainers.image.source="https://github.com/python-telegram-bot/python-telegram-bot" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

RUN --mount=type=secret,id=mitm_ca,required=0 \\
    if [ -f /run/secrets/mitm_ca ]; then \\
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && update-ca-certificates; \\
    fi

WORKDIR /home/

RUN set -eux; \\
    if ! apt-get update; then \\
        for src in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \\
            if [ -f "$src" ]; then \\
                sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g; s|ftp.debian.org|archive.debian.org|g; /-updates/d; /-backports/d' "$src"; \\
            fi; \\
        done; \\
        printf 'Acquire::Check-Valid-Until "false";\\n' > /etc/apt/apt.conf.d/99no-check-valid-until; \\
        apt-get update; \\
    fi; \\
    apt-get install -y --no-install-recommends ca-certificates git build-essential; \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN git clone "${REPO_URL}" /home/python-telegram-bot && \\
    cd /home/python-telegram-bot && git rev-parse HEAD >/dev/null

WORKDIR /home/python-telegram-bot

CMD ["/bin/bash"]
"""


class ImageDefault4682To4671(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return ImageBase4682To4671(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
set -eu
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "ERROR: not inside a git repository" >&2
    exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is not clean" >&2
    git status --porcelain >&2
    exit 1
fi
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euxo pipefail
cd /home/python-telegram-bot
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git fetch --no-tags origin {sha}
git checkout --detach {sha}
bash /home/check_git_changes.sh
git submodule update --init --recursive
pip install --no-cache-dir -e ".[all]"
pip install --no-cache-dir -r requirements-unit-tests.txt
pip install --no-cache-dir "pytest-timeout>=2,<3"
pip install --no-cache-dir pytz
python -c "import telegram"
python -m pytest --version
python -m pytest --collect-only -q tests/ > /home/collect.log 2>&1 || (tail -40 /home/collect.log; exit 1)
""".format(sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/python-telegram-bot
python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --timeout=180 tests/
""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/python-telegram-bot
if ! git -C /home/python-telegram-bot apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --timeout=180 tests/
""",
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/python-telegram-bot
if ! git -C /home/python-telegram-bot apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
python -m pytest -v --no-header -rA --tb=no -p no:cacheprovider --continue-on-collection-errors --timeout=180 tests/
""",
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        dep = self.dependency()
        return f"""# syntax=docker/dockerfile:1.6
FROM {dep.image_name()}:{dep.image_tag()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/python-telegram-bot; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/python-telegram-bot/.gitmodules ]; then \\
        cd /home/python-telegram-bot && git submodule foreach --recursive ' \\
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


@Instance.register("python-telegram-bot", "python_telegram_bot_4682_to_4671")
class PYTHON_TELEGRAM_BOT_4682_TO_4671(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault4682To4671(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        status_first = re.compile(
            r"^(?:\[gw\d+\]\s*)?(?:\[\s*\d+%\]\s*)?"
            r"(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(tests/.+?)(?:\s+-\s.*)?$"
        )
        name_first = re.compile(
            r"^(tests/.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s.*)?$"
        )

        for raw_line in log.splitlines():
            line = raw_line.strip()
            match = status_first.match(line)
            if match:
                status, name = match.group(1), match.group(2)
            else:
                match = name_first.match(line)
                if not match:
                    continue
                name, status = match.group(1), match.group(2)

            name = name.strip()
            if status in ("PASSED", "XPASS"):
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

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
