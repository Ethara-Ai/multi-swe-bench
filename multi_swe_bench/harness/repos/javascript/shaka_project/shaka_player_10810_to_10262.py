import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "shaka_player_10810_to_10262"
BASE_TAG = f"base-{_INTERVAL}"

CHECK_GIT_CHANGES = """#!/bin/bash
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

KARMA_CI_CONF = """const path = require('path');

function MsweReporter(baseReporterDecorator) {
  baseReporterDecorator(this);
  const write = (status, result) => {
    const name = result.suite.concat([result.description])
        .map((part) => String(part).replace(/\\s+/g, ' ').trim())
        .join(' > ');
    process.stdout.write(`MSWE_RESULT ${status} :: ${name}\\n`);
  };
  this.onSpecComplete = (browser, result) => {
    if (result.skipped || result.pending || result.disabled) {
      write('SKIPPED', result);
    } else if (result.success) {
      write('PASSED', result);
    } else {
      write('FAILED', result);
      for (const log of result.log || []) {
        process.stdout.write(`MSWE_LOG ${String(log).split('\\n')[0]}\\n`);
      }
    }
  };
  this.onBrowserError = (browser, error) => {
    process.stdout.write(`MSWE_BROWSER_ERROR ${JSON.stringify(error)}\\n`);
  };
  this.onRunComplete = (browsers, results) => {
    process.stdout.write(
        `MSWE_SUMMARY success=${results.success} failed=${results.failed} ` +
        `error=${results.error} disconnected=${results.disconnected}\\n`);
  };
}
MsweReporter.$inject = ['baseReporterDecorator'];

module.exports = (config) => {
  require(path.join(process.cwd(), 'karma.conf.js'))(config);
  config.set({
    basePath: process.cwd(),
    plugins: config.plugins.concat([
      require('/opt/karma-ci/node_modules/karma-chrome-launcher'),
      {'reporter:mswe': ['type', MsweReporter]},
    ]),
    customLaunchers: Object.assign({}, config.customLaunchers, {
      ChromiumHeadlessCI: {
        base: 'ChromeHeadless',
        flags: [
          '--no-sandbox',
          '--disable-gpu',
          '--disable-dev-shm-usage',
          '--autoplay-policy=no-user-gesture-required',
        ],
      },
    }),
    browsers: ['ChromiumHeadlessCI'],
    reporters: ['mswe'],
  });
};
"""

RUN_TESTS_PY = """import logging
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), 'build'))
import test

logging.getLogger().setLevel(logging.INFO)
launcher = test.Launcher('mswe')
launcher.ParseArguments(['--no-colors'])
launcher.ResolveBrowsers(['ChromiumHeadlessCI'])
sys.exit(launcher.RunCommand('/home/karma-ci.conf.js'))
"""

ENV_BLOCK = """export CI=true
export TZ=UTC
export CHROME_BIN=/usr/bin/chromium
export NODE_OPTIONS=--max-old-space-size=4096
export npm_config_update_notifier=false
"""

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
"""
)

APPLY_FN = r"""
apply_patches() {
    local excl=() f
    for f in $(awk '/^diff --git /{p=$3; sub(/^a\//,"",p)} /^Binary files .* differ$/{if(p!="")print p}' "$@" | sort -u); do
        excl+=("--exclude=$f")
        echo "apply_patches: excluding $f (binary change with no payload in the patch)"
    done
    git apply --whitespace=nowarn "${excl[@]}" "$@"
    for f in "${excl[@]#--exclude=}"; do
        if [ -f "/home/binaries/${f}" ]; then
            mkdir -p "$(dirname "${f}")"
            cp "/home/binaries/${f}" "${f}"
            echo "apply_patches: restored ${f} from /home/binaries"
        fi
    done
}
"""

TEST_COMMAND = "python3 /home/run_tests.py\n"

PREPARE_SH = (
    """#!/bin/bash
set -e

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __SHA__
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

write_test_helpers() {
    cat > /home/karma-ci.conf.js <<'__KARMA_CI_CONF_EOF__'
"""
    + KARMA_CI_CONF
    + """__KARMA_CI_CONF_EOF__
    cat > /home/run_tests.py <<'__RUN_TESTS_PY_EOF__'
"""
    + RUN_TESTS_PY
    + """__RUN_TESTS_PY_EOF__
}

write_test_helpers

npm ci || true
touch node_modules/.last_update

python3 build/gendeps.py
test -x node_modules/.bin/karma
test -f node_modules/google-closure-compiler-java/compiler.jar
node -e "const fs = require('fs'); for (const m of ['karma', 'karma-jasmine', 'karma-jasmine-ajax', 'jasmine-ajax', 'karma-sourcemap-loader', '@joeyparrish/karma-babel-preprocessor', 'google-closure-library', 'google-closure-deps']) { if (!fs.existsSync('node_modules/' + m + '/package.json')) { console.error('missing ' + m); process.exit(1); } } require.resolve('/opt/karma-ci/node_modules/karma-chrome-launcher'); console.log('karma deps ok');"
test -s /home/karma-ci.conf.js
test -s /home/run_tests.py
node --check /home/karma-ci.conf.js
python3 -m py_compile /home/run_tests.py
node --version
java -version
chromium --version
echo "DEPS_OK"
"""
)

RUN_SH = RUN_HEADER + "\n" + TEST_COMMAND

TEST_RUN_SH = RUN_HEADER + APPLY_FN + "apply_patches /home/test.patch\n\n" + TEST_COMMAND

FIX_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch /home/fix.patch\n\n"
    + TEST_COMMAND
)


def _payloadless_binaries(*patches: str) -> list[tuple[str, str]]:
    found = []
    for patch in patches:
        for chunk in re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE):
            if not re.search(r"^Binary files .* differ$", chunk, re.MULTILINE):
                continue
            path = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
            index = re.search(r"^index [0-9a-f]+\.\.([0-9a-f]+)", chunk, re.MULTILINE)
            if (
                not path
                or not index
                or re.search(r"^Binary files .* and /dev/null differ$", chunk, re.MULTILINE)
            ):
                continue
            item = (path.group(1), index.group(1))
            if item not in found:
                found.append(item)
    return found


class ShakaPlayerImageBase_10810_TO_10262(Image):
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
        return "node:22.23.4-trixie"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {self.dependency()}

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
    CI=true \\
    CHROME_BIN=/usr/bin/chromium \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image (karma + headless chromium)" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
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

RUN apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    ca-certificates \\
    chromium \\
    fonts-liberation \\
    git \\
    openjdk-21-jre-headless \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN mkdir -p /opt/karma-ci && cd /opt/karma-ci && \\
    npm init -y > /dev/null && \\
    npm install --no-audit --no-fund karma-chrome-launcher@3.2.0

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class ShakaPlayerImageDefault_10810_TO_10262(Image):
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
        return ShakaPlayerImageBase_10810_TO_10262(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return script.replace("__REPO__", self.pr.repo).replace(
            "__SHA__", self.pr.base.sha
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(PREPARE_SH)),
            File(".", "run.sh", self._render(RUN_SH)),
            File(".", "test-run.sh", self._render(TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        binaries = _payloadless_binaries(self.pr.test_patch, self.pr.fix_patch)
        stage_binaries = ""
        if binaries:
            steps = "".join(
                f'    mkdir -p "/home/binaries/$(dirname "{path}")"; \\\n'
                f'    blob="$(git rev-parse "FETCH_HEAD:{path}")"; \\\n'
                f'    case "${{blob}}" in {index}*) ;; *) echo "blob mismatch for {path}"; exit 1 ;; esac; \\\n'
                f'    git cat-file blob "${{blob}}" > "/home/binaries/{path}"; \\\n'
                for path, index in binaries
            )
            stage_binaries = (
                "RUN set -eux; \\\n"
                f"    cd /home/{self.pr.repo}; \\\n"
                f'    git fetch --no-tags origin "pull/{self.pr.number}/head"; \\\n'
                + steps
                + "    rm -f .git/FETCH_HEAD; \\\n"
                + '    test "$(git rev-parse HEAD)" = "${BASE_COMMIT}"\n'
            )

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{copy_commands}
RUN bash /home/prepare.sh

{stage_binaries}

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
    git -c pack.threads=1 -c pack.windowMemory=64m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates .git/ORIG_HEAD .git/FETCH_HEAD; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"; \\
    test -z "$(git remote)"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git -c pack.threads=1 -c pack.windowMemory=64m gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("shaka-project", "shaka-player")
@Instance.register("shaka-project", _INTERVAL)
class SHAKA_PLAYER_10810_TO_10262(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ShakaPlayerImageDefault_10810_TO_10262(self.pr, self._config)

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

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
        result_re = re.compile(r"^MSWE_RESULT (PASSED|FAILED|SKIPPED) :: (.+?)\s*$")

        seen: dict[str, int] = {}

        for line in clean_log.splitlines():
            match = result_re.match(line.strip())
            if not match:
                continue
            status, name = match.groups()
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name} [{seen[name]}]"
            if status == "PASSED":
                passed_tests.add(name)
            elif status == "FAILED":
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
