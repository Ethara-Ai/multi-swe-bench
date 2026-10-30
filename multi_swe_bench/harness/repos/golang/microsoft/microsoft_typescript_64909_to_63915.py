import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# microsoft/TypeScript PRs #63915 - #64909: the Go-port era of the repo
# (go.work over ./tsc and ./tools, npm workspaces over packages/*).
# Every base commit in this range pins the same toolchain:
#   package.json  engines.node ">=22.18", volta node 24.20.0 / npm 11.19.1,
#                 packageManager npm@11.19.1
#   go.work, tsc/go.mod, tools/go.mod, .github/actions/setup-go: go 1.27
# so one shared base image serves the whole era.
#
# Test surface (mirrors .github/workflows/ci.yml `test` + `test-extra`):
#   go test ./...                      in tsc/    (hereby test:tsc)
#   go test ./...                      in tools/  (hereby test:tools)
#   node --test ./test/**/*.test.ts    in packages/typescript (hereby test:api)
#   node --test tools/scripts/{gen,tsc}/*.test.*               (hereby test:codegen)
#   npm test                           in packages/vscode-typescript (hereby test:extension)
_INTERVAL_NAME = "microsoft_typescript_64909_to_63915"
_LO, _HI = 63915, 64909

_NODE_IMAGE = "node:24.20.0-bookworm"
_NPM_VERSION = "11.19.1"
_GO_VERSION = "1.27.3"
_GO_SHA256 = {
    "amd64": "65efbc80fdc1fa2cd2631cad5b481777a68e8725f34e89f50fc6ac714e379b40",
    "arm64": "a06e99309dbcbdca1a8ccd2282c3ee146e55930886e26b4f1193269587165f49",
}

# Binary hunks in these patches carry no full index line (`Binary files ...
# differ`), so git cannot apply them. They are the localized diagnostic
# archives (tsc/internal/diagnostics/loc/*.json.gz), which no test reads from
# the tree.
_GIT_APPLY = "git apply --whitespace=nowarn --exclude='*.gz'"

# Go test environment, exported identically in prepare.sh (which warms the
# build cache) and every stage, so cache keys match. GODEBUG is what hereby's
# goTestEnv sets; CGO_ENABLED=1 is the linux/gcc default CI tests run under,
# made explicit (the tree has no cgo, but the setting is part of the key).
_GO_TEST_ENV = "export CGO_ENABLED=1\nexport GODEBUG=tracebackancestors=10"

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

# Reads `go test -json` on stdin, prints one stable line per result:
#   MSWB_GO <PASS|FAIL|SKIP> <pkg>::<Test>/<subtest>
#   MSWB_GO_PKG <PASS|FAIL|SKIP> <pkg>   (package aggregate, not graded)
# A test that started but never reported (panic, timeout) is FAIL. Output of
# failing tests and failed builds is echoed, capped, for debugging.
_GO_REPORT_MJS = r"""import readline from "node:readline";

const MODULE_PREFIX = "github.com/microsoft/TypeScript/";
const MAX_LINES_PER_TEST = 40;
let dumpBudget = 20000;
const STATUS = { pass: "PASS", fail: "FAIL", skip: "SKIP" };

const pkgName = p => (p && p.startsWith(MODULE_PREFIX) ? p.slice(MODULE_PREFIX.length) : p || "<unknown>");
const clean = s => String(s).replace(/\s+/g, " ").trim();

const open = new Map();
const output = new Map();
const buildOutput = new Map();
let anyFail = false;

function remember(map, key, line) {
    let lines = map.get(key);
    if (!lines) map.set(key, (lines = []));
    if (lines.length < MAX_LINES_PER_TEST) lines.push(line);
}

function emit(status, name) {
    if (status === "FAIL") anyFail = true;
    process.stdout.write(`MSWB_GO ${status} ${name}\n`);
}

function dump(lines) {
    for (const l of lines || []) {
        if (dumpBudget-- <= 0) return;
        process.stdout.write(`    | ${String(l).replace(/\n$/, "")}\n`);
    }
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const raw of rl) {
    let e;
    try {
        e = JSON.parse(raw);
    }
    catch {
        if (raw.trim()) process.stdout.write(`MSWB_GO_RAW ${raw}\n`);
        continue;
    }
    const action = e.Action;
    if (action === "build-output") {
        remember(buildOutput, e.ImportPath, e.Output || "");
        continue;
    }
    if (action === "build-fail") {
        process.stdout.write(`MSWB_GO_BUILD_FAIL ${pkgName(e.ImportPath)}\n`);
        dump(buildOutput.get(e.ImportPath));
        continue;
    }
    const pkg = pkgName(e.Package);
    const test = e.Test ? clean(e.Test) : "";
    if (action === "run" && test) {
        if (!open.has(pkg)) open.set(pkg, new Set());
        open.get(pkg).add(test);
        continue;
    }
    if (action === "output") {
        remember(output, `${pkg}\0${test}`, e.Output || "");
        continue;
    }
    const status = STATUS[action];
    if (!status) continue;
    if (test) {
        open.get(pkg)?.delete(test);
        emit(status, `${pkg}::${test}`);
        if (status === "FAIL") dump(output.get(`${pkg}\0${test}`));
        output.delete(`${pkg}\0${test}`);
        continue;
    }
    for (const t of open.get(pkg) || []) emit("FAIL", `${pkg}::${t}`);
    open.delete(pkg);
    for (const key of output.keys()) if (key.startsWith(`${pkg}\0`) && key !== `${pkg}\0`) output.delete(key);
    if (status === "FAIL") anyFail = true;
    process.stdout.write(`MSWB_GO_PKG ${status} ${pkg}\n`);
    if (status === "FAIL") dump(output.get(`${pkg}\0`));
    output.delete(`${pkg}\0`);
}
for (const [pkg, tests] of open) for (const t of tests) emit("FAIL", `${pkg}::${t}`);
process.exitCode = anyFail ? 1 : 0;
"""

# node:test reporter, one stable line per finished test:
#   MSWB_NODE <PASS|FAIL|SKIP> <repo-relative file> > <suite> > ... > <test>
# No durations or line numbers, so names are identical across stages. A file
# that fails to load is reported under its own path.
_NODE_REPORTER_MJS = r"""import path from "node:path";

const root = process.env.MSWB_REPO_ROOT || process.cwd();
const stacks = new Map();

function rel(file) {
    if (!file) return "<unknown>";
    const f = file.startsWith("file://") ? new URL(file).pathname : file;
    return path.relative(root, path.resolve(f)).split(path.sep).join("/");
}

const clean = s => String(s).replace(/\s+/g, " ").trim();

export default async function* reporter(source) {
    for await (const event of source) {
        const data = event.data || {};
        if (event.type === "test:start") {
            const file = rel(data.file);
            const stack = stacks.get(file) || [];
            stack.length = data.nesting;
            stack[data.nesting] = clean(data.name);
            stacks.set(file, stack);
            continue;
        }
        if (event.type !== "test:pass" && event.type !== "test:fail") continue;
        const file = rel(data.file);
        const parts = [...(stacks.get(file) || []).slice(0, data.nesting), clean(data.name)];
        const name = parts.length === 1 && rel(parts[0]) === file ? file : `${file} > ${parts.join(" > ")}`;
        let status = event.type === "test:pass" ? "PASS" : "FAIL";
        if (data.skip !== undefined || data.todo !== undefined) status = "SKIP";
        yield `MSWB_NODE ${status} ${name}\n`;
        if (status === "FAIL" && data.details && data.details.error) {
            const err = data.details.error;
            yield `MSWB_NODE_ERR ${clean((err.cause && err.cause.message) || err.message || err).slice(0, 500)}\n`;
        }
    }
}
"""


def _run_tests_sh(repo: str) -> str:
    # Identical for run.sh / test-run.sh / fix-run.sh. Every suite runs even if
    # an earlier one fails; each failure is recorded and returned at the end.
    # Setup steps report as `MSWB_SETUP <PASS|FAIL> <name>` for diagnosis
    # only; parse_log does not grade them. A suite whose setup failed
    # contributes no per-test results.
    return f"""#!/bin/bash
set -eo pipefail

REPO=/home/{repo}
cd "$REPO"
export CI=true
export MSWB_REPO_ROOT="$REPO"
{_GO_TEST_ENV}
status=0

setup() {{
    local name="$1"; shift
    if "$@"; then
        echo "MSWB_SETUP PASS $name"
    else
        echo "MSWB_SETUP FAIL $name"
        status=1
        return 1
    fi
}}

go_suite() {{
    if (cd "$REPO/$1" && go test -count=1 -timeout=60m -json ./... 2>&1) | node /home/mswb_go_report.mjs; then
        :
    else
        status=1
    fi
}}

node_tests() {{
    local dir="$1"; shift
    # Reporter flags go first: node treats anything after the test files as
    # arguments to the test scripts, not as runner options.
    if (cd "$dir" && node --test-reporter=/home/mswb_node_reporter.mjs --test-reporter-destination=stdout "$@"); then
        :
    else
        status=1
    fi
}}

echo "=== MSWB suite: go tsc"
go_suite tsc

echo "=== MSWB suite: go tools"
go_suite tools

echo "=== MSWB suite: api"
if setup tsgo npx hereby tsgo && setup build-api-test npx hereby build:api:test; then
    node_tests "$REPO/packages/typescript" --conditions @typescript/source --test './test/**/*.test.ts'
fi

echo "=== MSWB suite: codegen"
shopt -s nullglob
codegen_tests=(tools/scripts/gen/*.test.mts tools/scripts/tsc/*.test.ts)
shopt -u nullglob
if [ "${{#codegen_tests[@]}}" -gt 0 ]; then
    node_tests "$REPO" --test --test-concurrency=1 "${{codegen_tests[@]}}"
fi

echo "=== MSWB suite: extension"
# `npm test` compiles and bundles before `node --test`; if it fails having
# reported no test at all, that was the build half, so say so.
ext_log=$(mktemp)
if (cd "$REPO/packages/vscode-typescript" && \\
    NODE_OPTIONS="${{NODE_OPTIONS:-}} --test-reporter=/home/mswb_node_reporter.mjs --test-reporter-destination=stdout" npm test) | tee "$ext_log"; then
    echo "MSWB_SETUP PASS build-extension-test"
else
    status=1
    if grep -q '^MSWB_NODE ' "$ext_log"; then
        echo "MSWB_SETUP PASS build-extension-test"
    else
        echo "MSWB_SETUP FAIL build-extension-test"
    fi
fi
rm -f "$ext_log"

echo "=== MSWB done status=$status"
exit "$status"
"""


class TypeScript64909To63915ImageBase(Image):
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
        return f"base-{_LO}_to_{_HI}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # SHARED base for the whole era: keeps full history and never pins or
        # scrubs. The leading syntax directive opts out of DockerfileEnhancer,
        # so all proxy / CA / label infrastructure is written out by hand.
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

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
    FORCE_COLOR=0 \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false \\
    PATH=/usr/local/go/bin:/root/go/bin:${{PATH}} \\
    CI=true

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
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

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl python3 make g++ \\
    && rm -rf /var/lib/apt/lists/*

# Go {_GO_VERSION} (every go.mod / go.work in this era declares go 1.27).
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) sha="{_GO_SHA256["amd64"]}" ;; \\
        arm64) sha="{_GO_SHA256["arm64"]}" ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/go.tgz "https://go.dev/dl/go{_GO_VERSION}.linux-$arch.tar.gz"; \\
    echo "$sha  /tmp/go.tgz" | sha256sum -c -; \\
    tar -C /usr/local -xzf /tmp/go.tgz; \\
    rm /tmp/go.tgz; \\
    go version

# npm pinned by package.json `packageManager` / `volta` across the era.
RUN npm install -g npm@{_NPM_VERSION} && npm --version

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1

{code}

{self.clear_env}

CMD ["/bin/bash"]
"""


class TypeScript64909To63915ImageDefault(Image):
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
        return TypeScript64909To63915ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mswb_go_report.mjs", _GO_REPORT_MJS),
            File(".", "mswb_node_reporter.mjs", _NODE_REPORTER_MJS),
            File(".", "mswb_run_tests.sh", _run_tests_sh(self.pr.repo)),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}

# Section 1: PIN
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

# Section 2: PROVISION
export CI=true
{go_env}
npm ci --no-audit --no-fund
go -C tsc mod download
go -C tools mod download
# Warm the Go build cache with every test package of both modules, so the
# three stages only recompile what the patches touch.
go -C tsc test -count=1 -run='^$' ./... > /dev/null
go -C tools test -count=1 -run='^$' ./... > /dev/null
# Same artifacts the stages rebuild before the API tests (hereby test:api).
npx hereby tsgo
npx hereby build:api:test
# Generators may rewrite tracked files; restore them so the stage patches
# apply to the pristine base tree. Untracked build output is kept.
git checkout -- .

# Section 3: GATE
test -x built/local/tsc
built/local/tsc --version
test -x node_modules/.bin/hereby
go -C tsc list -test ./... > /dev/null
go -C tools list -test ./... > /dev/null
echo DEPS_OK

""".format(pr=self.pr, go_env=_GO_TEST_ENV),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
bash /home/mswb_run_tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
{git_apply} /home/test.patch
bash /home/mswb_run_tests.sh

""".format(pr=self.pr, git_apply=_GIT_APPLY),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
export CI=true
{git_apply} /home/test.patch /home/fix.patch
bash /home/mswb_run_tests.sh

""".format(pr=self.pr, git_apply=_GIT_APPLY),
            ),
        ]

    def dockerfile(self) -> str:
        # dependency() returns an Image, so DockerfileEnhancer never touches
        # this file: the pin and the history scrub are written out here.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        repo = self.pr.repo

        copy_commands = "COPY " + " ".join(file.name for file in self.files()) + " /home/"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout --detach {sha}

{copy_commands}

RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git config --local pack.threads 2; \\
    git config --local pack.windowMemory 256m; \\
    git config --local pack.deltaCacheSize 128m; \\
    git config --local core.bigFileThreshold 16m; \\
    git gc --prune=now; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

CMD ["/bin/bash"]
"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# MSWB_SETUP lines are build steps, not tests, and are deliberately not matched.
_RESULT_RE = re.compile(r"^MSWB_(?:GO|NODE) (PASS|FAIL|SKIP) (\S.*?)\s*$")


def _parse_mswb_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}
    for line in _ANSI_RE.sub("", test_log).splitlines():
        m = _RESULT_RE.match(line.strip())
        if not m:
            continue
        status, name = m.groups()
        buckets[status].add(name)

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


@Instance.register("microsoft", _INTERVAL_NAME)
class TYPESCRIPT_64909_TO_63915(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return TypeScript64909To63915ImageDefault(self.pr, self._config)

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


# ---------------------------------------------------------------------------
# Routing
#
# The JSONL carries no number_interval, so Instance.create() looks up the bare
# "microsoft/TypeScript" key. This module owns that key and routes by PR
# number: this era's range comes here, everything else goes to the pre-Go-port
# TypeScript.py config. That module is imported here (not looked up in the
# registry), so its registration has always run before ours overrides the
# key, whatever order microsoft/__init__.py imports the two files in.
# ---------------------------------------------------------------------------

from multi_swe_bench.harness.repos.golang.microsoft.TypeScript import (  # noqa: E402
    TypeScript as _PRE_GO_PORT,
)


@Instance.register("microsoft", "TypeScript")
class TYPESCRIPT_64909_TO_63915_DISPATCH(Instance):
    """Router only: __new__ returns the era-appropriate instance."""

    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        if _LO <= pr.number <= _HI:
            return TYPESCRIPT_64909_TO_63915(pr, config, *args, **kwargs)
        return _PRE_GO_PORT(pr, config, *args, **kwargs)
