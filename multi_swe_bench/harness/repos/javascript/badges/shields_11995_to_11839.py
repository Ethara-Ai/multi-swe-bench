from __future__ import annotations

import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PR_LOW = 11995
PR_HIGH = 11839
INTERVAL = f"{PR_LOW}_to_{PR_HIGH}"
INTERVAL_NAME = f"shields_{INTERVAL}"

NODE_IMAGE = "node:24-bookworm"
NPM_VERSION = "11.19.1"

REPORTER_PATH = "/home/mocha-ci-reporter.cjs"
RESULT_PREFIX = "###TEST_RESULT:"
RUN_COMPLETE_PREFIX = "###MOCHA_RUN_COMPLETE:"

# Same command in all three stages: service testers are selected from the
# files test.patch touches, filtered to those present in the tree. Separate
# mocha processes keep a load-time crash in one suite from hiding the others.
TEST_CMD = r"""node --version
npm --version
test -x node_modules/.bin/mocha

overall_status=0
run_suite() {
    suite="$1"
    shift
    suite_status=0
    node_modules/.bin/mocha \
        --reporter [[REPORTER_PATH]] \
        --timeout 30000 \
        --exit \
        "$@" || suite_status=$?
    echo "###MOCHA_EXIT:$suite:$suite_status"
    if [ "$suite_status" -ne 0 ]; then
        overall_status=1
    fi
}

run_suite core "core/**/*.spec.js" "lib/**/*.spec.js" "services/**/*.spec.js"
run_suite package "badge-maker/**/*.spec.js"
run_suite entrypoint entrypoint.spec.js

service_ids=""
for tester in $(sed -n 's#^+++ b/\(services/.*\.tester\.js\)$#\1#p' /home/test.patch | sort -u); do
    [ -f "$tester" ] || continue
    if ids=$(node --input-type=module -e 'const m = await import(process.argv[1]); for (const v of Object.values(m)) { if (v && typeof v.id === "string") console.log(v.id) }' "$PWD/$tester" 2>/dev/null); then
        for id in $ids; do
            service_ids="$service_ids,$id"
        done
    fi
done
service_ids="${service_ids#,}"
echo "###SERVICE_TESTERS:$service_ids"
if [ -n "$service_ids" ]; then
    export RETRY_COUNT=3
    run_suite services core/service-test-runner/cli.js "--only=$service_ids"
fi

echo "###MOCHA_EXIT:$overall_status"
exit "$overall_status"
"""

PRUNE = r"""RUN set -eux; \
    cd /home/[[REPO]]; \
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git config --local pack.threads 2; \
    git config --local pack.windowMemory 256m; \
    git config --local pack.deltaCacheSize 128m; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "[[SHA]]"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/[[REPO]]/.gitmodules ]; then \
        cd /home/[[REPO]] && git submodule foreach --recursive ' \
            git checkout --detach HEAD; \
            git remote remove origin 2>/dev/null || true; \
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \
                | xargs -r -n1 git update-ref -d; \
            git reflog expire --expire=now --all; \
            git reflog expire --expire-unreachable=now --all; \
            git gc --prune=now --aggressive; \
            rm -f .git/objects/info/alternates; \
        '; \
    fi"""

CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1 || {
  echo "check_git_changes: Not inside a git repository"
  exit 1
}

test -z "$(git status --porcelain)" || {
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
}

echo "check_git_changes: No uncommitted changes"
exit 0
"""

REPORTER_CJS = r"""'use strict'

const fs = require('fs')

const RESULT_PREFIX = '[[RESULT_PREFIX]]'
const RUN_COMPLETE_PREFIX = '[[RUN_COMPLETE_PREFIX]]'

const EVENT_TEST_PASS = 'pass'
const EVENT_TEST_FAIL = 'fail'
const EVENT_TEST_PENDING = 'pending'
const EVENT_RUN_END = 'end'

function write(text) {
  const buf = Buffer.from(text, 'utf8')
  let offset = 0
  while (offset < buf.length) {
    try {
      offset += fs.writeSync(1, buf, offset, buf.length - offset)
    } catch (err) {
      if (err.code === 'EAGAIN') {
        continue
      }
      throw err
    }
  }
}

function fullName(test) {
  const path =
    typeof test.titlePath === 'function' ? test.titlePath() : [test.title]
  return path
    .map(part => String(part == null ? '' : part).replace(/\s+/g, ' ').trim())
    .filter(part => part.length > 0)
    .join(' > ')
}

function emit(status, test) {
  const name = fullName(test)
  if (!name) {
    return
  }
  write(RESULT_PREFIX + status + ':' + name + '\n')
}

module.exports = function MochaCiReporter(runner) {
  let passed = 0
  let failed = 0
  let skipped = 0

  runner.on(EVENT_TEST_PASS, function (test) {
    passed += 1
    emit('PASS', test)
  })

  runner.on(EVENT_TEST_PENDING, function (test) {
    skipped += 1
    emit('SKIP', test)
  })

  runner.on(EVENT_TEST_FAIL, function (test) {
    failed += 1
    emit('FAIL', test)
  })

  runner.on(EVENT_RUN_END, function () {
    write(RUN_COMPLETE_PREFIX + passed + ':' + failed + ':' + skipped + '\n')
  })
}
"""

PREPARE_SH = r"""#!/bin/bash
set -e

export CI=true
export HUSKY=0

cd /home/[[REPO]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund --loglevel=error \
    || npm ci --no-audit --no-fund --loglevel=error \
    || npm install --no-audit --no-fund --loglevel=error

node --version
npm --version
test -d node_modules
test -x node_modules/.bin/mocha
node --input-type=module -e "await import('mocha'); await import('chai'); await import('nock'); await import('sazerac'); await import('joi'); await import('re2'); await import('icedfrisby'); await import('icedfrisby-nock'); await import('is-svg'); console.log('IMPORTS_OK')"

printf 'describe("gate", function () {\n  it("works", function () {})\n})\n' > /tmp/mocha-gate.spec.cjs
gate_out=$(node_modules/.bin/mocha --reporter [[REPORTER_PATH]] --exit /tmp/mocha-gate.spec.cjs)
rm -f /tmp/mocha-gate.spec.cjs
printf '%s\n' "$gate_out" | grep -q '^[[RESULT_PREFIX]]PASS:gate > works$'
printf '%s\n' "$gate_out" | grep -q '^[[RUN_COMPLETE_PREFIX]]1:0:0$'
echo DEPS_OK
"""

STAGE_SH = r"""#!/bin/bash
set -eo pipefail

export CI=true
export TZ=UTC
export NODE_CONFIG_ENV=test
export HANDLE_INTERNAL_ERRORS=false
export FORCE_COLOR=0
export NO_COLOR=1
export NODE_OPTIONS="${NODE_OPTIONS:-} --dns-result-order=ipv4first"

cd /home/[[REPO]]

[[PATCH_STEP]]
[[TEST_CMD]]
"""

TEST_PATCH_STEP = "git apply --whitespace=nowarn /home/test.patch\n"

FIX_PATCH_STEP = """git apply --whitespace=nowarn /home/test.patch /home/fix.patch

if ! git diff --quiet HEAD -- package.json package-lock.json; then
    npm install --no-audit --no-fund --no-save --loglevel=error
fi
"""

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RESULT_RE = re.compile(
    r"^.*?" + re.escape(RESULT_PREFIX) + r"(PASS|FAIL|SKIP):(.+?)\s*$"
)
WIDE_INT_RE = re.compile(r"(?<![\w.])\d{10,13}(?![\w.])")
WIDE_INT_PLACEHOLDER = "<n>"


class SHIELDS_11995_TO_11839_ImageBase(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{INTERVAL}"

    def workdir(self) -> str:
        return f"base-{INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\
    CYPRESS_INSTALL_BINARY=0 \\
    npm_config_audit=false \\
    npm_config_fund=false \\
    npm_config_update_notifier=false \\
    HUSKY=0 \\
    NODE_CONFIG_ENV=test \\
    FORCE_COLOR=0 \\
    NO_COLOR=1 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    CI=true

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
        git ca-certificates curl python3 make g++ pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g npm@{NPM_VERSION}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD > /dev/null

CMD ["/bin/bash"]
"""


class SHIELDS_11995_TO_11839_ImageDefault(Image):
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
        return SHIELDS_11995_TO_11839_ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[TEST_CMD]]", TEST_CMD)
            .replace("[[REPO]]", self.pr.repo)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[REPORTER_PATH]]", REPORTER_PATH)
            .replace("[[RESULT_PREFIX]]", RESULT_PREFIX)
            .replace("[[RUN_COMPLETE_PREFIX]]", RUN_COMPLETE_PREFIX)
        )

    def _stage(self, patch_step: str) -> str:
        return self._expand(STAGE_SH).replace("[[PATCH_STEP]]", patch_step)

    def install_files(self) -> list[File]:
        return [
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "mocha-ci-reporter.cjs", self._expand(REPORTER_CJS)),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
        ]

    def grading_files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "run.sh", self._stage("")),
            File(".", "test-run.sh", self._stage(TEST_PATCH_STEP)),
            File(".", "fix-run.sh", self._stage(FIX_PATCH_STEP)),
        ]

    def files(self) -> list[File]:
        return self.install_files() + self.grading_files()

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        def copies(files: list[File]) -> str:
            return "".join(f"COPY {f.name} /home/\n" for f in files)

        return f"""FROM {name}:{tag}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copies(self.install_files())}
{copies(self.grading_files())}
RUN bash /home/prepare.sh

{self._expand(PRUNE)}

{self.clear_env}
"""


@Instance.register("badges", INTERVAL_NAME)
class SHIELDS_11995_TO_11839(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return SHIELDS_11995_TO_11839_ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = ANSI_RE.sub("", test_log)

        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()
        occurrences: dict[str, int] = {}

        for line in log.splitlines():
            match = RESULT_RE.match(line.rstrip())
            if not match:
                continue

            status = match.group(1)
            name = re.sub(r"\s+", " ", match.group(2)).strip()
            name = WIDE_INT_RE.sub(WIDE_INT_PLACEHOLDER, name)
            if not name:
                continue

            occurrences[name] = occurrences.get(name, 0) + 1
            if occurrences[name] > 1:
                name = f"{name} #{occurrences[name]}"

            if status == "FAIL":
                failed.add(name)
            elif status == "SKIP":
                skipped.add(name)
            else:
                passed.add(name)

        passed -= failed
        passed -= skipped
        skipped -= failed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )


Instance.register("badges", "shields")(SHIELDS_11995_TO_11839)
Instance.register("badges", INTERVAL)(SHIELDS_11995_TO_11839)
