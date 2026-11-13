import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:24-bookworm"
_INTERVAL_NAME = "vue_vben_admin_8442_to_8382"
_REGISTRY = "https://registry.npmjs.org/"

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
    LC_ALL=C.UTF-8 \\
    TZ=Atlantic/Reykjavik \\
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
    NODE_OPTIONS=--max-old-space-size=4096 \\
    HUSKY=0 \\
    LEFTHOOK=0 \\
    CYPRESS_INSTALL_BINARY=0 \\
    PUPPETEER_SKIP_DOWNLOAD=1 \\
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \\
    NODE_COMPILE_CACHE=/tmp/node-compile-cache \\
    npm_config_registry=https://registry.npmjs.org/ \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_AUDIT=false
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

retry() {{
  n=0
  until [ "$n" -ge 5 ]; do
    "$@" && return 0
    n=$((n + 1))
    echo "prepare: attempt $n failed, retrying: $*"
    sleep 3
  done
  return 1
}}

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || git fetch --no-tags origin "+${{BASE_COMMIT}}:refs/heads/msb-base-commit"
git checkout --detach "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

PNPM_VERSION=$(node -p "((require('./package.json').packageManager || 'pnpm@11').split('@')[1] || '11').split('+')[0]")
retry npm install -g "pnpm@${{PNPM_VERSION}}" --registry={registry}
retry pnpm install --frozen-lockfile --registry={registry}
retry pnpm -r run --if-present stub

test -f vitest.config.ts
test -x node_modules/.bin/vitest
node -e "require.resolve('vitest/package.json'); require.resolve('happy-dom/package.json'); require.resolve('vue/package.json')"
retry npx vitest list > /tmp/mswb_gate_list.log 2>&1 || {{ tail -n 60 /tmp/mswb_gate_list.log; exit 1; }}
test -s /tmp/mswb_gate_list.log
echo DEPS_OK
"""

_RUN_TESTS_SH = """#!/bin/bash
MODE="$1"
cd /home/{repo}
LIST=/tmp/mswb_specs.txt
rm -f "$LIST"
{{
  find . -type f -name '*.test.ts'
  find . -type f -name '*.test.tsx'
  find . -type f -name '*.test.js'
  find . -type f -name '*.test.mjs'
  find . -type f -name '*.spec.ts'
  find . -type f -name '*.spec.tsx'
}} | grep -vF '/node_modules/' \\
   | grep -vF '/dist/' \\
   | grep -vF '/e2e/' \\
   | grep -vF '/coverage/' \\
   | grep -vF '/.turbo/' \\
   | grep -vF '/.output/' \\
   | grep -vF '/.nitro/' \\
   | grep -vF '/.git/' \\
   | cut -c3- | sort -u > "$LIST"
COUNT=$(wc -l < "$LIST" | tr -d ' ')
echo "MSWB_EXPECTED_SPECS $COUNT"
if [ "$COUNT" = "0" ]; then
  echo "MSWB_NO_SPECS"
  exit 1
fi
RESULTS=/tmp/mswb_results.txt
: > "$RESULTS"
while read -r spec; do
  [ -z "$spec" ] && continue
  out=/tmp/mswb_vitest_$(echo "$spec" | tr '/.' '__').json
  rm -f "$out"
  npx vitest run "$spec" --retry=2 --reporter=json --outputFile="$out" > /tmp/mswb_vitest.log 2>&1
  node /home/mswb_digest.js "$spec" "$out" > /tmp/mswb_one.txt 2>&1
  rc=$?
  cat /tmp/mswb_one.txt
  cat /tmp/mswb_one.txt >> "$RESULTS"
  if [ "$rc" != "0" ]; then
    echo "MSWB_SUITE_LOG_BEGIN $spec"
    tail -n 40 /tmp/mswb_vitest.log
    echo "MSWB_SUITE_LOG_END $spec"
  fi
done < "$LIST"
EMITTED=$(grep -c '^MSWB_RESULT ' "$RESULTS" || true)
echo "MSWB_EMITTED_RESULTS $EMITTED"
if [ "$EMITTED" = "0" ]; then
  echo "MSWB_ALL_SUITES_FAILED"
  exit 1
fi
SUITE_FAILURES=$(grep -c '^MSWB_SUITE_FAILED ' "$RESULTS" || true)
UNNAMED=$(grep -c '::SUITE_ERROR' "$RESULTS" || true)
echo "MSWB_SUITE_FAILURES $SUITE_FAILURES"
echo "MSWB_UNNAMED_SUITES $UNNAMED"
if [ "$MODE" = "strict" ] && [ "$SUITE_FAILURES" != "0" ]; then
  echo "MSWB_STRICT_UNPATCHED_SUITE_FAILURES"
  exit 1
fi
exit 0
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
bash /home/run_tests.sh strict
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn --exclude=pnpm-lock.yaml /home/test.patch
bash /home/run_tests.sh
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{repo}
git reset --hard
git clean -fdq
git apply --whitespace=nowarn --exclude=pnpm-lock.yaml /home/test.patch /home/fix.patch
bash /home/run_tests.sh
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -eo pipefail

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_MSWB_DIGEST_JS = r"""const fs = require('fs');

const spec = process.argv[2];
const out = process.argv[3];

function emit(status, name) {
  console.log('MSWB_RESULT ' + status + ' ' + spec + '::' + name);
}

function expectedNames() {
  let src;
  try {
    src = fs.readFileSync(spec, 'utf8');
  } catch (e) {
    return [];
  }
  if (/\b(?:describe|it|test)\.each\b/.test(src)) {
    return [];
  }
  const names = [];
  const stack = [];
  let depth = 0;
  for (const raw of src.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const m = line.match(
      /^\s*(describe|it|test)(?:\.\w+)*\s*\(\s*(['"`])([\s\S]*?)\2/
    );
    let kind = null;
    let title = null;
    if (m) {
      kind = m[1];
      title = m[3];
      if (m[2] === '`' && title.indexOf('${') !== -1) {
        return [];
      }
    }
    const opens = (line.match(/[({]/g) || []).length;
    const closes = (line.match(/[)}]/g) || []).length;
    if (kind === 'describe') {
      stack.push([title, depth]);
    } else if (kind === 'it' || kind === 'test') {
      names.push(
        stack
          .map((s) => s[0])
          .concat([title])
          .join(' > ')
      );
    }
    depth += opens - closes;
    while (stack.length && depth <= stack[stack.length - 1][1]) {
      stack.pop();
    }
  }
  return names;
}

function suiteError() {
  console.log('MSWB_SUITE_FAILED ' + spec);
  const names = expectedNames();
  if (names.length) {
    for (const n of names) {
      emit('FAILED', n);
    }
  } else {
    emit('FAILED', 'SUITE_ERROR');
  }
  process.exit(1);
}

if (!fs.existsSync(out)) {
  suiteError();
}

let report;
try {
  report = JSON.parse(fs.readFileSync(out, 'utf8'));
} catch (e) {
  suiteError();
}

let count = 0;
for (const file of report.testResults || []) {
  for (const t of file.assertionResults || []) {
    const name = [...(t.ancestorTitles || []), t.title]
      .map((x) => String(x).trim())
      .filter((x) => x.length > 0)
      .join(' > ');
    const status =
      t.status === 'passed'
        ? 'PASSED'
        : t.status === 'failed'
          ? 'FAILED'
          : 'SKIPPED';
    emit(status, name);
    count += 1;
  }
}

if (count === 0) {
  suiteError();
}
"""

_RESULT_RE = re.compile(r"^MSWB_RESULT (PASSED|FAILED|SKIPPED) (.+?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests = set()
    failed_tests = set()
    skipped_tests = set()
    test_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)
    for line in test_log.splitlines():
        match = _RESULT_RE.match(line.strip())
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if status == "PASSED":
            passed_tests.add(name)
        elif status == "FAILED":
            failed_tests.add(name)
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        infra = _INFRA_BLOCK.format(org=self.pr.org, repo=self.pr.repo)
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {_NODE_IMAGE}\n\n"
            f"{infra}\n"
            f"{_LANG_ENV}\n"
            "RUN git --version && node --version && npm --version\n\n"
            "WORKDIR /home/\n\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    git ca-certificates jq \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
            "RUN git config --global --add safe.directory '*'\n\n"
            f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
            f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null\n\n"
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
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH.format(repo=repo, registry=_REGISTRY)),
            File(".", "run.sh", _RUN_SH.format(repo=repo)),
            File(".", "test-run.sh", _TEST_RUN_SH.format(repo=repo)),
            File(".", "fix-run.sh", _FIX_RUN_SH.format(repo=repo)),
            File(".", "run_tests.sh", _RUN_TESTS_SH.format(repo=repo)),
            File(".", "mswb_digest.js", _MSWB_DIGEST_JS),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        hardening = Image._HARDENING_BLOCK.rstrip("\n")
        return (
            "# syntax=docker/dockerfile:1.6\n\n"
            f"FROM {base.image_name()}:{base.image_tag()}\n\n"
            f"{copy_commands}\n"
            f'ARG BASE_COMMIT="{self.pr.base.sha}"\n\n'
            "RUN bash /home/prepare.sh\n\n"
            f"WORKDIR /home/{self.pr.repo}\n\n"
            f"{hardening}\n"
        )


@Instance.register("vbenjs", _INTERVAL_NAME)
class VUE_VBEN_ADMIN_8442_TO_8382(Instance):
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
        return _parse_mswb_log(test_log)


Instance.register("vbenjs", "vue-vben-admin")(VUE_VBEN_ADMIN_8442_TO_8382)
