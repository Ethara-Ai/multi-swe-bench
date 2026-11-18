import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_IMAGE = "debian:bookworm"
NODE_VERSION = "22.23.4"
BUN_VERSION = "1.4.2"
BASE_TAG = "base-3777_to_3777"
RUN_USER = "runner"
PLAYWRIGHT_DIR = "/opt/ms-playwright"

_ENV = f"""\
export CI=true
export TZ=UTC
export NODE_OPTIONS=--max-old-space-size=4096
export PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_DIR}
export HUSKY=0
export DISPLAY=:99
"""

_DROP_ROOT = f"""\
if [ "$(id -u)" = 0 ]; then
  chown -R {RUN_USER}:{RUN_USER} /home/REPO
  exec runuser -u {RUN_USER} -- bash "$0" "$@"
fi
"""

_BUILD_STEPS = [
    "bun run gen:skill-docs --host all",
    "bun run vendor:xterm",
    "bash browse/scripts/build-node-server.sh",
    "bun run build:gates",
    "bun run build:cso",
]

_BUILD_CMD = "\n".join(_BUILD_STEPS)

_REBUILD = "if ! { " + " && ".join(_BUILD_STEPS) + "; } 2>&1; then\n  echo 'MSB_BUILD|FAIL'\nfi"

_LIST_CMD = """\
bun --no-env-file -e 'const m = await import(process.cwd() + "/scripts/test-free-shards.ts"); const ex = m.TREE_MUTATING || {}; for (const f of m.collectFreeTestFiles()) console.log((f in ex ? "X " : "P ") + f);'"""

_RUN_CHUNK = """\
#!/bin/bash
chunk="$1"
sandbox="$(mktemp -d)"
mkdir -p "$sandbox/tmp"
args=()
while IFS= read -r f; do
  [ -n "$f" ] && args+=("$PWD/$f")
done < "$chunk"
TMPDIR="$sandbox/tmp" TEMP="$sandbox/tmp" TMP="$sandbox/tmp" CHROMIUM_PROFILE="$sandbox/chromium-profile" \\
  timeout -k 30 1800 bun test "${args[@]}" --timeout=30000 --max-concurrency=1 \\
  --reporter=junit --reporter-outfile="$chunk.xml" > "$chunk.log" 2>&1
echo "$?" > "$chunk.rc"
cat "$chunk.log"
rm -rf "$sandbox"
"""

_EMIT_RESULTS = """\
node - "$LIST_DIR" <<'JS'
const fs = require('fs');
const path = require('path');
const dir = process.argv[2];
const root = process.cwd();
const decode = (s) => s.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|amp|lt|gt|quot|apos);/g, (m, e) => {
  if (e === 'amp') return '&';
  if (e === 'lt') return '<';
  if (e === 'gt') return '>';
  if (e === 'quot') return '"';
  if (e === 'apos') return "'";
  return String.fromCodePoint(e[1] === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
});
const attr = (tag, name) => {
  const m = new RegExp('\\\\s' + name + '="([^"]*)"').exec(tag || '');
  return m ? decode(m[1]) : '';
};
const rel = (f) => (path.isAbsolute(f) ? path.relative(root, f) : f).split(path.sep).join('/').replace(/^\\.\\//, '');
const clean = (s) => s.replace(/\\s+/g, ' ').trim();
const chunks = fs.readdirSync(dir).filter((n) => /^(chunk|solo)\\.[a-z]+$/.test(n)).sort();
for (const chunk of chunks) {
  const files = fs.readFileSync(path.join(dir, chunk), 'utf8').split('\\n').filter(Boolean);
  const seen = new Set();
  const xml = path.join(dir, chunk + '.xml');
  if (fs.existsSync(xml)) {
    const text = fs.readFileSync(xml, 'utf8');
    const stack = [];
    const re = /<(\\/?)(testsuite|testcase|failure|error|skipped)\\b([^>]*?)(\\/?)>/g;
    let tc = null;
    const emit = (t) => {
      if (!t || stack.length === 0) return;
      const file = rel(attr(stack[0], 'file') || attr(stack[0], 'name'));
      let parents = stack.slice(1).map((a) => attr(a, 'name'));
      const cls = attr(t.attrs, 'classname');
      if (parents.length === 0 && cls && cls !== attr(stack[0], 'name')) parents = [cls];
      const name = clean([...parents, attr(t.attrs, 'name')].join(' > '));
      seen.add(file);
      console.log(`MSB_TEST|${t.status}|${file}::${name}`);
    };
    let m;
    while ((m = re.exec(text))) {
      const [, close, tag, attrs, self] = m;
      if (tag === 'testsuite') {
        if (close) stack.pop();
        else if (!self) stack.push(attrs);
        continue;
      }
      if (tag === 'testcase') {
        if (close) { emit(tc); tc = null; continue; }
        tc = { attrs, status: 'PASS' };
        if (self) { emit(tc); tc = null; }
        continue;
      }
      if (tc && !close) tc.status = tag === 'skipped' && tc.status !== 'FAIL' ? 'SKIP' : 'FAIL';
    }
  }
  for (const f of files) if (!seen.has(f)) console.log(`MSB_TEST|FAIL|${f}`);
}
JS"""

_TEST_CMD = f"""\
RC=0
LIST_DIR="$(mktemp -d)"
{_LIST_CMD} > "$LIST_DIR/all.txt"
awk '/^P /{{print substr($0, 3)}}' "$LIST_DIR/all.txt" | split -l 10 - "$LIST_DIR/chunk."
awk '/^X /{{print substr($0, 3)}}' "$LIST_DIR/all.txt" | split -l 1 - "$LIST_DIR/solo."
cat > "$LIST_DIR/run-chunk.sh" <<'SH'
{_RUN_CHUNK}SH
JOBS="$(nproc)"
if [ "$JOBS" -gt 4 ]; then JOBS=4; fi
Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp > /dev/null 2>&1 &
XVFB_PID=$!
echo 'MSB_SUITE|bun'
find "$LIST_DIR" -maxdepth 1 -name 'chunk.*' ! -name '*.*.*' | LC_ALL=C sort | xargs -r -n1 -P "$JOBS" bash "$LIST_DIR/run-chunk.sh" || RC=1
for s in $(find "$LIST_DIR" -maxdepth 1 -name 'solo.*' ! -name '*.*.*' | LC_ALL=C sort); do
  bash "$LIST_DIR/run-chunk.sh" "$s" || RC=1
done
if ! kill "$XVFB_PID" 2> /dev/null; then echo 'Xvfb already stopped'; fi
for r in "$LIST_DIR"/*.rc; do
  if [ -f "$r" ] && [ "$(cat "$r")" != 0 ]; then RC=1; fi
done
{_EMIT_RESULTS}
exit $RC"""

_GATE_CMD = f"""\
bun --version
node --version
node -e "require.resolve('playwright')"
test -n "$(find {PLAYWRIGHT_DIR} -maxdepth 1 -name 'chromium-*' -print -quit)"
COUNT="$({_LIST_CMD} | grep -c .)"
test "$COUNT" -gt 0
echo DEPS_OK"""


_CHECK_GIT_CHANGES = """\
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
"""


class Gstack3777To3777ImageBase(Image):
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
        return BASE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()

        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD > /dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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
    GIT_SSL_CAINFO=${{CA_CERT_PATH}} \\
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}} \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    PLAYWRIGHT_BROWSERS_PATH={PLAYWRIGHT_DIR} \\
    HUSKY=0 \\
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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates curl unzip xz-utils python3 make g++ gcc libc6-dev \\
        jq bc file perl procps zsh poppler-utils xvfb x11-utils \\
        fonts-liberation fonts-noto-color-emoji fontconfig \\
    && fc-cache -f \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) node_arch=x64; bun_target=linux-x64-baseline ;; \\
        arm64) node_arch=arm64; bun_target=linux-aarch64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL --retry 5 --retry-delay 5 "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$node_arch.tar.xz" -o /tmp/node.tar.xz; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \\
    rm -f /tmp/node.tar.xz; \\
    curl -fsSL --retry 5 --retry-delay 5 "https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}/bun-$bun_target.zip" -o /tmp/bun.zip; \\
    unzip -q /tmp/bun.zip -d /tmp/bun; \\
    install -D -m 0755 "/tmp/bun/bun-$bun_target/bun" /usr/local/bin/bun; \\
    ln -sf bun /usr/local/bin/bunx; \\
    rm -rf /tmp/bun /tmp/bun.zip; \\
    node --version; \\
    npm --version; \\
    test "$(bun --version)" = "{BUN_VERSION}"

RUN useradd -m -s /bin/bash {RUN_USER} && \\
    mkdir -p {PLAYWRIGHT_DIR} && \\
    chmod 1777 /tmp

RUN git config --global --add safe.directory '*' && \\
    git config --system --add safe.directory '*' && \\
    git config --system user.email "msb@gstack.test" && \\
    git config --system user.name "MSB" && \\
    git config --system init.defaultBranch main

{code}

CMD ["/bin/bash"]
"""


class Gstack3777To3777ImageDefault(Image):
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
        return Gstack3777To3777ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        prepare_sh = f"""\
#!/bin/bash
set -euo pipefail

cd /home/REPO
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach BASE_SHA
bash /home/check_git_changes.sh

ENV_BLOCK
chown -R {RUN_USER}:{RUN_USER} /home/REPO
runuser -u {RUN_USER} -- bash -c 'set -euo pipefail; cd /home/REPO; bun install --frozen-lockfile'
npx --no-install playwright install --with-deps chromium
chmod -R a+rX {PLAYWRIGHT_DIR}
runuser -u {RUN_USER} -- bash -euo pipefail <<'AS_RUNNER'
cd /home/REPO
BUILD_CMD
GATE_CMD
AS_RUNNER
"""

        run_sh = """\
#!/bin/bash
set -eo pipefail
DROP_ROOT
ENV_BLOCK
cd /home/REPO
git checkout -q -B main
git checkout -q -- .
REBUILD
TEST_CMD
"""

        test_run_sh = """\
#!/bin/bash
set -eo pipefail
DROP_ROOT
ENV_BLOCK
cd /home/REPO
git checkout -q -B main
git checkout -q -- .
git apply --whitespace=nowarn /home/test.patch
REBUILD
TEST_CMD
"""

        fix_run_sh = """\
#!/bin/bash
set -eo pipefail
DROP_ROOT
ENV_BLOCK
cd /home/REPO
git checkout -q -B main
git checkout -q -- .
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
REBUILD
TEST_CMD
"""

        def render(text: str) -> str:
            return (
                text.replace("DROP_ROOT\n", _DROP_ROOT)
                .replace("ENV_BLOCK\n", _ENV)
                .replace("REBUILD", _REBUILD)
                .replace("BUILD_CMD", _BUILD_CMD)
                .replace("TEST_CMD", _TEST_CMD)
                .replace("GATE_CMD", _GATE_CMD)
                .replace("BASE_SHA", self.pr.base.sha)
                .replace("/home/REPO", f"/home/{repo}")
            )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", render(prepare_sh)),
            File(".", "run.sh", render(run_sh)),
            File(".", "test-run.sh", render(test_run_sh)),
            File(".", "fix-run.sh", render(fix_run_sh)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git cat-file -e "${{BASE_COMMIT}}^{{commit}}" 2>/dev/null || \\
    git fetch --no-tags origin "${{BASE_COMMIT}}"
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git -c pack.threads=2 -c pack.windowMemory=512m gc --prune=now --aggressive; \\
    git -c pack.threads=2 -c pack.windowMemory=512m repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "${{BASE_COMMIT}}"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}
"""


@Instance.register("garrytan", "gstack_3777_to_3777")
class GSTACK_3777_TO_3777(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return Gstack3777To3777ImageDefault(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        buckets = {"PASS": passed_tests, "FAIL": failed_tests, "SKIP": skipped_tests}

        re_suite = re.compile(r"^MSB_SUITE\|(\S+)\s*$")
        re_test = re.compile(r"^MSB_TEST\|(PASS|FAIL|SKIP)\|(.+?)\s*$")

        suite = ""
        for line in log.splitlines():
            line = line.strip()
            m = re_suite.match(line)
            if m:
                suite = m.group(1)
                continue
            if not suite:
                continue
            m = re_test.match(line)
            if m:
                buckets[m.group(1)].add(m.group(2))

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
