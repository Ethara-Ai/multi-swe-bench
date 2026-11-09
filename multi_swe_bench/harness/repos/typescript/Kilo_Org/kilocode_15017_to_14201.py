import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "kilocode_15017_to_14201"
_NODE_IMAGE = "node:24.22.0-bookworm"
_TEST_TIMEOUT_MS = "60000"

_DIFF_FILE = re.compile(r"^diff --git a/\S+ b/(\S+)$", re.MULTILINE)
_TEST_FILE = re.compile(r"^(?:[^/]+/)*[^/]+(?:\.test|_test)\.(?:c|m)?[jt]sx?$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RESULT = re.compile(r"^BUN_RESULT (?P<status>PASSED|FAILED|SKIPPED) (?P<name>\S+::.+)$")

_RUN_ENV = """export CI=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=8192
export KILO_EXPERIMENTAL_DISABLE_FILEWATCHER=true
export ELECTRON_SKIP_BINARY_DOWNLOAD=1
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export npm_config_update_notifier=false
export npm_config_fund=false
export npm_config_audit=false
"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_JUNIT_RESULTS = r"""const fs = require("fs");

const [xmlPath, prefix = ""] = process.argv.slice(2);
if (!xmlPath || !fs.existsSync(xmlPath)) {
  process.exit(0);
}
const xml = fs.readFileSync(xmlPath, "utf8");

const decode = (s) =>
  s
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");

const attr = (tag, name) => {
  const m = tag.match(new RegExp(`\\s${name}="([^"]*)"`));
  return m ? decode(m[1]) : "";
};

const clean = (s) => s.replace(/\s+/g, " ").trim();

const suites = [];
const seen = new Map();
let current = null;

const emit = (tc) => {
  const path = [...suites.slice(1), tc.name].map(clean).filter(Boolean).join(" > ");
  const key = `${prefix}${tc.file}::${path}`;
  const n = (seen.get(key) || 0) + 1;
  seen.set(key, n);
  const name = n === 1 ? key : `${key} [${n}]`;
  process.stdout.write(`BUN_RESULT ${tc.status} ${name}\n`);
};

const tokens = xml.match(/<testsuite\b[^>]*>|<\/testsuite>|<testcase\b[^>]*>|<\/testcase>|<failure\b|<error\b|<skipped\b/g) || [];
for (const token of tokens) {
  if (token.startsWith("<testsuite")) {
    if (!token.endsWith("/>")) {
      suites.push(attr(token, "name"));
    }
  } else if (token === "</testsuite>") {
    suites.pop();
  } else if (token.startsWith("<testcase")) {
    const tc = { name: attr(token, "name"), file: attr(token, "file"), status: "PASSED" };
    if (token.endsWith("/>")) {
      emit(tc);
    } else {
      current = tc;
    }
  } else if (token === "</testcase>") {
    if (current) {
      emit(current);
    }
    current = null;
  } else if (current && (token === "<failure" || token === "<error")) {
    current.status = "FAILED";
  } else if (current && token === "<skipped" && current.status !== "FAILED") {
    current.status = "SKIPPED";
  }
}
"""

_RUN_TESTS = """#!/bin/bash
set -eo pipefail

ROOT=/home/{repo}
STATUS=0
declare -A PKG_FILES=()

for f in "$@"; do
    if [ ! -f "$ROOT/$f" ]; then
        continue
    fi
    d="$(dirname "$f")"
    while [ "$d" != "." ] && [ ! -f "$ROOT/$d/package.json" ]; do
        d="$(dirname "$d")"
    done
    if [ "$d" = "." ]; then
        rel="$f"
    else
        rel="${{f#"$d"/}}"
    fi
    PKG_FILES["$d"]="${{PKG_FILES["$d"]:-}} ./$rel"
done

for d in $(printf '%s\\n' "${{!PKG_FILES[@]}}" | sort); do
    key="$(printf '%s' "$d" | tr '/.' '__')"
    out="/tmp/bun-junit-$key.xml"
    rm -f "$out"
    if [ "$d" = "." ]; then
        prefix=""
    else
        prefix="$d/"
    fi
    cd "$ROOT/$d"
    bun test --timeout {timeout} --reporter=junit --reporter-outfile="$out" ${{PKG_FILES["$d"]}} || STATUS=$?
    bun /home/bun-junit-results.js "$out" "$prefix"
done

cd "$ROOT"
exit $STATUS
"""


def _patch_files(patch: str) -> list[str]:
    files = []
    for path in _DIFF_FILE.findall(patch or ""):
        if path not in files:
            files.append(path)
    return files


def _test_files(pr: PullRequest) -> list[str]:
    return [f for f in _patch_files(pr.test_patch) if _TEST_FILE.match(f)]


def _test_command(pr: PullRequest) -> str:
    return "bash /home/run-tests.sh " + " ".join(_test_files(pr)) + "\n"


class Kilocode15017To14201ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {_NODE_IMAGE}

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

RUN git config --global --add safe.directory '*' && \\
    git config --global user.email "test@example.com" && \\
    git config --global user.name "test"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class Kilocode15017To14201ImageDefault(Image):
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
        return Kilocode15017To14201ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_command = _test_command(self.pr)
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "bun-junit-results.js", _JUNIT_RESULTS),
            File(
                ".",
                "run-tests.sh",
                _RUN_TESTS.format(repo=repo, timeout=_TEST_TIMEOUT_MS),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {base_sha}
bash /home/check_git_changes.sh

{run_env}
BUN_VERSION="$(node -p "require('./package.json').packageManager.split('@')[1]")"
npm install -g "bun@$BUN_VERSION"
bun install --frozen-lockfile

test "$(bun --version)" = "$BUN_VERSION"
bun -e "require.resolve('turbo/package.json'); require.resolve('@typescript/native-preview/package.json'); console.log('DEPS_OK', Bun.version)"
""".format(repo=repo, base_sha=self.pr.base.sha, run_env=_RUN_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch

{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}
{run_env}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_command}""".format(repo=repo, run_env=_RUN_ENV, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

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


@Instance.register("Kilo-Org", "kilocode")
@Instance.register("Kilo-Org", _INTERVAL)
class KILOCODE_15017_TO_14201(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Kilocode15017To14201ImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        for raw in _ANSI.sub("", log).splitlines():
            match = _RESULT.match(raw.strip())
            if not match:
                continue
            name = match.group("name").strip()
            status = match.group("status")
            if status == "FAILED":
                failed_tests.add(name)
            elif status == "PASSED":
                passed_tests.add(name)
            else:
                skipped_tests.add(name)

        passed_tests -= failed_tests
        skipped_tests -= passed_tests | failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
