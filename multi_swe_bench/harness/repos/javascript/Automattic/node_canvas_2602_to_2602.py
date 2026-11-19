from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

BASE_TAG = "base-2602_to_2602"

NODE_IMAGE = "node:22-bookworm"

REPO_DIR = "/home/node-canvas"

ADDON_VERSION = "4.0.0-rc3"
ADDON_PACKAGES = {
    "amd64": "canvas-linux-x64-gnu",
    "arm64": "canvas-linux-arm64-gnu",
}
ADDON_DIR = "/opt/canvas-addon"

ARIMO_PACKAGE = "@expo-google-fonts/arimo"
ARIMO_VERSION = "0.4.3"
ARIMO_MEMBER = "package/400Regular/Arimo_400Regular.ttf"
ARIMO_FIXTURE = "test/fixtures/Arimo-Regular.ttf"
ARIMO_CACHE = "/opt/arimo/Arimo-Regular.ttf"

REPORT_MARKER = "@@NODE_CANVAS_TEST_JSON@@"

RUN_TEST_NAME = "<run>"

MOCHA_TIMEOUT_MS = 20000

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)


def _patch_blocks(patch: str) -> list[tuple[str, str]]:
    blocks = []
    for block in re.split(r"(?m)^(?=diff --git )", patch or ""):
        header = _DIFF_HEADER_RE.match(block)
        if header:
            blocks.append((header.group(2), block))
    return blocks


def binary_stub_paths(pr: PullRequest) -> list[str]:
    stubs = set()
    for patch in (pr.test_patch, pr.fix_patch):
        for path, block in _patch_blocks(patch):
            if re.search(r"(?m)^Binary files .* differ$", block):
                stubs.add(path)
    return sorted(stubs)


class NodeCanvasImageBase(Image):
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
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        org = self.pr.org
        repo = self.pr.repo
        amd64_pkg = ADDON_PACKAGES["amd64"]
        arm64_pkg = ADDON_PACKAGES["arm64"]

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
    CI=true \\
    NODE_OPTIONS="--max-old-space-size=4096" \\
    npm_config_fund=false \\
    npm_config_audit=false \\
    npm_config_update_notifier=false

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
        git ca-certificates curl \\
        libcairo2-dev libjpeg-dev libpango1.0-dev libgif-dev librsvg2-dev \\
        build-essential g++ make python3 pkg-config \\
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \\
    case "${{TARGETARCH:-$(dpkg --print-architecture)}}" in \\
        amd64) addon_pkg={amd64_pkg} ;; \\
        arm64) addon_pkg={arm64_pkg} ;; \\
        *) echo "no prebuilt canvas addon for ${{TARGETARCH}}" >&2; exit 1 ;; \\
    esac; \\
    mkdir -p {ADDON_DIR}; \\
    cd /tmp; \\
    npm pack "${{addon_pkg}}@{ADDON_VERSION}"; \\
    tar -xzf "${{addon_pkg}}-{ADDON_VERSION}.tgz" -C {ADDON_DIR} --strip-components=1; \\
    rm -f "${{addon_pkg}}-{ADDON_VERSION}.tgz"; \\
    test -f {ADDON_DIR}/canvas.node

RUN set -eux; \\
    mkdir -p "$(dirname {ARIMO_CACHE})"; \\
    cd /tmp; \\
    npm pack "{ARIMO_PACKAGE}@{ARIMO_VERSION}"; \\
    tar -xzf expo-google-fonts-arimo-{ARIMO_VERSION}.tgz "{ARIMO_MEMBER}"; \\
    cp "{ARIMO_MEMBER}" {ARIMO_CACHE}; \\
    rm -rf /tmp/package "/tmp/expo-google-fonts-arimo-{ARIMO_VERSION}.tgz"; \\
    test -s {ARIMO_CACHE}

RUN git config --global --add safe.directory '*'

RUN git clone "${{REPO_URL}}" {REPO_DIR}

WORKDIR {REPO_DIR}

{self.clear_env}

CMD ["/bin/bash"]
"""


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


PREPARE_SH = r"""#!/bin/bash
set -euo pipefail

cd [[REPO_DIR]]

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach [[SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[SHA]]"

node --version
npm --version

npm ci --build-from-source

test -f build/Release/canvas.node
node -e "const c = require('[[REPO_DIR]]'); if (typeof c.createCanvas !== 'function' || typeof c.Canvas !== 'function') { throw new Error('canvas did not load'); } const cv = c.createCanvas(8, 8); if (!cv.getContext('2d')) { throw new Error('no 2d context'); }"
node -e "require.resolve('mocha/package.json')"
test -f [[ADDON_DIR]]/canvas.node
test -s [[ARIMO_CACHE]]
echo DEPS_OK
"""


REPORT_CJS = r"""'use strict';
const fs = require('fs');

const ROOT = '[[REPO_DIR]]';
const [, , report, marker] = process.argv;

const tests = [];
const seen = new Map();

function add(name, status) {
  const n = (seen.get(name) || 0) + 1;
  seen.set(name, n);
  tests.push({name: n > 1 ? `${name} #${n}` : name, status});
}

function rel(p) {
  p = String(p || '');
  return p.startsWith(ROOT + '/') ? p.slice(ROOT.length + 1) : p;
}

let data = null;
try {
  if (fs.existsSync(report) && fs.statSync(report).size > 0) {
    data = JSON.parse(fs.readFileSync(report, 'utf8'));
  }
} catch (err) {
  process.stderr.write(`report: ${err.message}\n`);
}

if (!data) {
  add('[[RUN]]', 'failed');
} else {
  for (const [key, status] of [['passes', 'passed'], ['failures', 'failed'], ['pending', 'skipped']]) {
    for (const t of data[key] || []) {
      const file = rel(t.file);
      const title = String(t.fullTitle || t.title || '');
      add(file ? `${file}::${title}` : title, status);
    }
  }
}

console.log(marker);
console.log(JSON.stringify({canvas_tests: tests}));
"""


TEST_CMD = r"""_addon=[[ADDON_DIR]]/canvas.node
if [ -f "${_addon}" ]; then
    for _d in npm/linux-x64-gnu npm/linux-arm64-gnu; do
        [ -d "${_d}" ] && cp "${_addon}" "${_d}/canvas.node"
    done
    for _p in canvas-linux-x64-gnu canvas-linux-arm64-gnu; do
        if [ -d "node_modules/${_p}" ] || [ -d "npm/${_p#canvas-}" ]; then
            mkdir -p "node_modules/${_p}"
            cp "${_addon}" "node_modules/${_p}/canvas.node"
            [ -f "node_modules/${_p}/package.json" ] || \
                printf '{"name":"%s","version":"[[ADDON_VERSION]]","main":"canvas.node"}\n' "${_p}" \
                    > "node_modules/${_p}/package.json"
        fi
    done
fi

if [ -s [[ARIMO_CACHE]] ] && grep -q "Arimo-Regular.ttf" test/canvas.test.js 2>/dev/null; then
    mkdir -p "$(dirname [[ARIMO_FIXTURE]])"
    cp [[ARIMO_CACHE]] [[ARIMO_FIXTURE]]
fi

_report=/tmp/canvas-mocha.json
rm -f "${_report}"

_rc=0
npx mocha --timeout [[MOCHA_TIMEOUT]] --reporter json \
    --reporter-option output="${_report}" test/*.test.js || _rc=$?

node /home/canvas_report.cjs "${_report}" "[[REPORT_MARKER]]"
exit "${_rc}"
"""


class NodeCanvasImageDefault(Image):
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
        return NodeCanvasImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _expand(self, template: str) -> str:
        return (
            template.replace("[[REPO_DIR]]", REPO_DIR)
            .replace("[[SHA]]", self.pr.base.sha)
            .replace("[[ADDON_DIR]]", ADDON_DIR)
            .replace("[[ADDON_VERSION]]", ADDON_VERSION)
            .replace("[[ARIMO_CACHE]]", ARIMO_CACHE)
            .replace("[[ARIMO_FIXTURE]]", ARIMO_FIXTURE)
            .replace("[[MOCHA_TIMEOUT]]", str(MOCHA_TIMEOUT_MS))
            .replace("[[REPORT_MARKER]]", REPORT_MARKER)
            .replace("[[RUN]]", RUN_TEST_NAME)
        )

    def _apply(self, *patches: str) -> str:
        excludes = "".join(f" --exclude='{p}'" for p in binary_stub_paths(self.pr))
        return f"git apply --whitespace=nowarn{excludes} {' '.join(patches)}\n"

    def files(self) -> list[File]:
        stage_patches = {
            "run.sh": "",
            "test-run.sh": self._apply("/home/test.patch"),
            "fix-run.sh": self._apply("/home/test.patch", "/home/fix.patch"),
        }
        stages = [
            File(
                ".",
                name,
                self._expand(
                    "#!/bin/bash\nset -eo pipefail\nexport CI=true\n\n"
                    "cd [[REPO_DIR]]\n\n" + patch_step + "\n" + TEST_CMD
                ),
            )
            for name, patch_step in stage_patches.items()
        ]
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._expand(PREPARE_SH)),
            File(".", "canvas_report.cjs", self._expand(REPORT_CJS)),
        ] + stages

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{sha}"

{copies}
RUN bash /home/prepare.sh

WORKDIR {REPO_DIR}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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

{self.clear_env}
"""


@Instance.register("Automattic", "node_canvas_2602_to_2602")
class NODE_CANVAS_2602_TO_2602(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NodeCanvasImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        buckets: dict[str, set[str]] = {
            "passed": set(),
            "failed": set(),
            "skipped": set(),
        }

        report = None
        lines = log.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != REPORT_MARKER:
                continue
            for candidate in lines[index + 1 :]:
                candidate = candidate.strip()
                if not candidate.startswith("{"):
                    continue
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and "canvas_tests" in parsed:
                    report = parsed
                    break
            break

        for entry in (report or {}).get("canvas_tests") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            status = str(entry.get("status") or "")
            if name and status in buckets:
                buckets[status].add(name)

        passed = buckets["passed"]
        failed = buckets["failed"]
        skipped = buckets["skipped"]

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


Instance.register("Automattic", "node-canvas")(NODE_CANVAS_2602_TO_2602)
