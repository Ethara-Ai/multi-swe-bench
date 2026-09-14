import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

FAST_ELEMENT = "@microsoft/fast-element@0.18.0"

TEST_COMMAND = """cat > /tmp/wtr-reporter.config.mjs <<'WTRCFG'
import { createRequire } from 'module';
import base from '/home/[[REPO]]/web-test-runner.config.mjs';

const require = createRequire('/home/[[REPO]]/package.json');
const { chromeLauncher } = require('@web/test-runner-chrome');

function walk(file, suite, prefix, out, seen) {
  const p = prefix ? prefix + ' > ' + suite.name : suite.name;
  for (const t of suite.tests || []) {
    const status = t.skipped ? 'SKIPPED' : t.passed ? 'PASSED' : 'FAILED';
    const id = file + ' > ' + (p ? p + ' > ' : '') + t.name;
    const n = (seen.get(id) || 0) + 1;
    seen.set(id, n);
    out.push((n > 1 ? id + ' #' + n : id) + ' :: ' + status);
  }
  for (const s of suite.suites || []) walk(file, s, p, out, seen);
}

const nameReporter = {
  reportTestFileResults({ sessionsForTestFile, testFile }) {
    const rel = testFile.replace('/home/[[REPO]]/', '');
    const out = [];
    const seen = new Map();
    for (const s of sessionsForTestFile) {
      if (s.testResults) walk(rel, s.testResults, '', out, seen);
    }
    for (const l of out) console.log('TESTCASE ' + l);
  },
};

const browser = chromeLauncher({
  launchOptions: {
    executablePath: '/usr/bin/chromium',
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  },
});

export default { ...base, rootDir: '/home/[[REPO]]', browsers: [browser], reporters: [nameReporter] };
WTRCFG
cd /home/[[REPO]]
npx wtr --static-logging --coverage --config /tmp/wtr-reporter.config.mjs 2>&1"""


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
        return "node:14-bullseye"

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

RUN sed -i -e 's|deb.debian.org|archive.debian.org|g' \\
        -e 's|security.debian.org|archive.debian.org|g' \\
        -e '/bullseye-updates/d' \\
        -e '/bullseye-security/d' /etc/apt/sources.list && \\
    apt-get -o Acquire::Check-Valid-Until=false \\
        -o Acquire::AllowInsecureRepositories=true update && \\
    apt-get -o APT::Get::AllowUnauthenticated=true install -y \\
        --no-install-recommends --allow-unauthenticated \\
        git ca-certificates chromium fonts-liberation \\
    && rm -rf /var/lib/apt/lists/* \\
    && ln -sf /usr/bin/chromium /usr/bin/chromium-browser

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


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
        sha = self.pr.base.sha
        test_command = TEST_COMMAND.replace("[[REPO]]", repo)

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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

export CI=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/[[REPO]]

git reset --hard
git clean -fdq
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
git clean -fdq
bash /home/check_git_changes.sh

yarn --frozen-lockfile --network-concurrency 4 --network-timeout 600000
yarn add -W --network-concurrency 4 --network-timeout 600000 [[FAST_ELEMENT]]
git checkout -- package.json yarn.lock
yarn build:codegen

node -v
yarn -v
/usr/bin/chromium --version
test -x /usr/bin/chromium-browser
test -f packages/test-helpers/schema.ts
node -e "['@web/test-runner', '@web/test-runner-chrome', '@web/dev-server-esbuild', '@open-wc/testing', 'sinon', 'graphql', '@apollo/client/core', '@microsoft/fast-element', '@apollo-elements/test-helpers/package.json', '@apollo-elements/mixins/package.json'].forEach((m) => require.resolve(m)); console.log('DEPS_OK')"
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[FAST_ELEMENT]]", FAST_ELEMENT),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096

[[TEST_COMMAND]]
""".replace("[[TEST_COMMAND]]", test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "PATCH_APPLY_FAILED: test.patch does not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=test
export NODE_OPTIONS=--max-old-space-size=4096

cd /home/[[REPO]]
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "PATCH_APPLY_FAILED: test.patch + fix.patch do not apply at [[SHA]]" >&2
    exit 1
fi

[[TEST_COMMAND]]
""".replace("[[REPO]]", repo).replace("[[SHA]]", sha).replace("[[TEST_COMMAND]]", test_command),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        sha = self.pr.base.sha
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha)

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{repo}

RUN bash /home/prepare.sh

{hardening}

{self.clear_env}
"""


@Instance.register("apollo-elements", "apollo-elements")
class ApolloElementsApolloElements(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        line_re = re.compile(
            r"TESTCASE\s+(?P<name>.+?)\s+::\s+(?P<status>PASSED|FAILED|SKIPPED)\s*$"
        )

        for line in cleaned.splitlines():
            m = line_re.search(line.rstrip())
            if not m:
                continue
            name = m.group("name").strip()
            status = m.group("status")
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
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
