
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_NO_PRUNE_HARDENING = chr(10).join(
    line
    for line in Image._HARDENING_BLOCK.split(chr(10))
    if "git gc --prune=now" not in line and "git repack -a -d -l" not in line
)

_FALLBACK_NODE = "14"


_TEST_CMD = "yarn jest --ci --verbose --runInBand --watchAll=false {scope} 2>&1"


def _test_scope(pr: PullRequest) -> str:
    dirs = []
    for line in (pr.test_patch or "").split(chr(10)):
        if line.startswith("diff --git a/"):
            path = line.split(" a/", 1)[1].split(" b/", 1)[0]
            d = path.rsplit("/", 1)[0] if "/" in path else ""
            if d.endswith("/__snapshots__"):
                d = d[: -len("/__snapshots__")]
            if d and d not in dirs:
                dirs.append(d)
    return " ".join(dirs)


class GrafanaImageBase(Image):
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
        return "debian:bookworm"

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
        curl git jq ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/install.sh | bash

RUN git config --global --add safe.directory '*'
RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{_NO_PRUNE_HARDENING}

WORKDIR /home/

CMD ["/bin/bash"]
"""


class GrafanaImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return GrafanaImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                """\
#!/bin/bash
set -eo pipefail

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

if [ -f .nvmrc ]; then
    nvm install
    nvm use
else
    nvm install {fallback_node}
    nvm use {fallback_node}
fi
node --version

if grep -q '"packageManager"' package.json 2>/dev/null; then
    corepack enable
    corepack prepare --activate 2>/dev/null || true

    export CYPRESS_INSTALL_BINARY=0
    node -e '
      const fs = require("fs");
      const p = JSON.parse(fs.readFileSync("package.json", "utf8"));
      const meta = p.dependenciesMeta || Object.create(null);
      const off = Object.create(null);
      off.built = false;
      meta["cypress"] = off;
      meta["@parcel/watcher"] = off;
      p.dependenciesMeta = meta;
      fs.writeFileSync("package.json", JSON.stringify(p, null, 2));
    '

    yarn install --immutable || YARN_CHECKSUM_BEHAVIOR=update yarn install
else
    npm list -g yarn >/dev/null 2>&1 || npm install -g yarn@1
    yarn install --frozen-lockfile || yarn install
fi

""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    fallback_node=_FALLBACK_NODE,
                ),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
git apply --whitespace=nowarn --include='**/__mocks__/**' /home/fix.patch 2>/dev/null || true
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

export CI=true
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
nvm use >/dev/null 2>&1 || nvm use {fallback_node} >/dev/null 2>&1 || true

{test_cmd}

""".format(
                    repo=self.pr.repo,
                    fallback_node=_FALLBACK_NODE,
                    test_cmd=_TEST_CMD.format(scope=_test_scope(self.pr)),
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}

RUN bash /home/prepare.sh

{self.clear_env}

"""


@Instance.register("grafana", "grafana")
class Grafana(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GrafanaImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_go_pass = re.compile(r"^--- PASS: (\S+)")
        re_go_fail = re.compile(r"^--- FAIL: (\S+)")
        re_go_skip = re.compile(r"^--- SKIP: (\S+)")

        re_jest_suite = re.compile(r"^(PASS|FAIL)\s+(\S+)")

        re_jest_pass = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_fail = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
        re_jest_skip = re.compile(r"^\s*[○⊘]\s+(.+)")

        current_suite = ""
        has_individual_tests = False

        def get_base_name(name: str) -> str:
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).strip()
            if not line:
                continue

            m = re_go_pass.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_go_fail.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_go_skip.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

            m = re_jest_suite.match(line)
            if m:
                current_suite = m.group(2)
                if not has_individual_tests:
                    if m.group(1) == "PASS":
                        failed_tests.discard(current_suite)
                        skipped_tests.discard(current_suite)
                        passed_tests.add(current_suite)
                    else:
                        failed_tests.discard(current_suite)
                        passed_tests.discard(current_suite)
                        failed_tests.add(current_suite)
                continue

            m = re_jest_pass.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_jest_fail.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                passed_tests.discard(current_suite)
                failed_tests.discard(current_suite)
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_jest_skip.match(line)
            if m:
                has_individual_tests = True
                test_name = (
                    f"{current_suite} > {m.group(1)}" if current_suite else m.group(1)
                )
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
