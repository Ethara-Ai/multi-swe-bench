import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "paper_js_2421_to_2207"
BASE_TAG = f"base-{_INTERVAL}"
NODE_IMAGE = "node:24-bookworm"

CHECK_GIT_CHANGES = """#!/bin/bash
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

ENV_BLOCK = """export CI=true
export PHANTOMJS_PLATFORM=linux
export PHANTOMJS_ARCH=x64
export NODE_OPTIONS=--max-old-space-size=3072
"""

RUN_HEADER = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """export NODE_ENV=test
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

REINSTALL_BLOCK = """
changed="$(git diff --name-only HEAD; git ls-files --others --exclude-standard)"
if printf '%s\\n' "${changed}" | grep -E '(^|/)package\\.json$|^yarn\\.lock$|^\\.yarnrc\\.yml$' >/dev/null; then
    YARN_ENABLE_IMMUTABLE_INSTALLS=false node "$(ls .yarn/releases/yarn-*.cjs | head -n 1)" install
fi
"""

TEST_BLOCK = """
cat > /tmp/msb_qunit.cjs <<'MSB_EOF'
const path = require('path');
const root = process.argv[2];
const load = require('module').createRequire(path.join(root, 'package.json'));
const QUnit = load('qunitjs');
Object.assign(global, QUnit);
global.QUnit = QUnit;
let failed = 0;
QUnit.log(function (d) {
    if (!d.result) console.log('MSB_ASSERT_FAIL ' + d.module + ' :: ' + d.name + ' :: ' + (d.message || ''));
});
QUnit.testDone(function (d) {
    const status = d.skipped ? 'SKIP' : (d.failed ? 'FAIL' : 'PASS');
    if (d.failed) failed++;
    console.log('MSB_TEST ' + status + ' ' + d.module + ' :: ' + d.name);
});
QUnit.done(function (d) {
    console.log('MSB_DONE total=' + d.total + ' failed=' + d.failed + ' passed=' + d.passed);
    process.exitCode = failed ? 1 : 0;
});
process.on('uncaughtException', function (e) {
    console.log('MSB_ERROR ' + ((e && e.stack) || e));
    process.exitCode = 1;
});
function requirePath(file, namespace) {
    const exported = load(file);
    if (namespace) global[namespace] = exported;
    else Object.assign(global, exported);
}
process.chdir(path.join(root, 'test'));
requirePath(path.join(root, 'node_modules', 'prepro', 'lib', 'node.js'));
requirePath(path.join(root, 'dist', 'paper-full.js'), 'paper');
requirePath(path.join(root, 'test', 'load.js'));
QUnit.load();
MSB_EOF
node /tmp/msb_qunit.cjs "$(pwd)"
"""

PREPARE_SH = (
    """#!/bin/bash
set -eo pipefail

"""
    + ENV_BLOCK
    + """
cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "__SHA__^{commit}" 2>/dev/null; then
    git fetch origin "__SHA__"
fi
git checkout --detach "__SHA__"
test "$(git rev-parse HEAD)" = "__SHA__"
bash /home/check_git_changes.sh

corepack enable
node "$(ls .yarn/releases/yarn-*.cjs | head -n 1)" install --immutable
node -e "require.resolve('qunitjs'); require.resolve('prepro/lib/node.js'); require('canvas'); console.log('DEPS_OK')"

"""
)

RUN_SH = RUN_HEADER + TEST_BLOCK

TEST_RUN_SH = RUN_HEADER + APPLY_FN + "apply_patches /home/test.patch\n" + REINSTALL_BLOCK + TEST_BLOCK

FIX_RUN_SH = (
    RUN_HEADER
    + APPLY_FN
    + "apply_patches /home/test.patch /home/fix.patch\n"
    + REINSTALL_BLOCK
    + TEST_BLOCK
)


def _payloadless_binaries(*patches: str) -> list[tuple[str, str]]:
    found = []
    for patch in patches:
        for chunk in re.split(r"^(?=diff --git )", patch, flags=re.MULTILINE):
            if not re.search(r"^Binary files .* differ$", chunk, re.MULTILINE):
                continue
            path = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
            index = re.search(r"^index [0-9a-f]+\.\.([0-9a-f]+)", chunk, re.MULTILINE)
            if not path or not index or re.search(r"^Binary files .* and /dev/null differ$", chunk, re.MULTILINE):
                continue
            item = (path.group(1), index.group(1))
            if item not in found:
                found.append(item)
    return found


class PaperJsYarnImageBase_2421_TO_2207(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6
FROM {NODE_IMAGE}

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

RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do \\
        if [ -f "$f" ]; then sed -i 's|http://deb.debian.org|https://deb.debian.org|g' "$f"; fi; \\
    done && \\
    apt-get -o Acquire::Retries=5 update && \\
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    pkg-config \\
    fonts-liberation \\
    libcairo2-dev \\
    libpango1.0-dev \\
    libjpeg-dev \\
    libgif-dev \\
    librsvg2-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PaperJsYarnImageDefault_2421_TO_2207(Image):
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
        return PaperJsYarnImageBase_2421_TO_2207(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, script: str) -> str:
        return script.replace("__REPO__", self.pr.repo).replace("__SHA__", self.pr.base.sha)

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
WORKDIR /home/{self.pr.repo}

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


@Instance.register("paperjs", _INTERVAL)
class PAPER_JS_2421_TO_2207(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PaperJsYarnImageDefault_2421_TO_2207(self.pr, self._config)

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
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        test_re = re.compile(r"^MSB_TEST (?P<status>PASS|FAIL|SKIP) (?P<name>.+)$")

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line).strip()
            match = test_re.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            if match.group("status") == "PASS":
                passed_tests.add(name)
            elif match.group("status") == "FAIL":
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
