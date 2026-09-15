from __future__ import annotations

from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log

_PR_LOW, _PR_HIGH = 46035, 51297

NODE_IMAGE = "node:18"

_BASE_IMAGE_TAG = "base-51297_to_46035"

TEST_CMD = (
    r"""FILES=$(sed -n 's#^diff --git a/\([^ ]*\) b/.*#\1#p' /home/test.patch); """
    r"""DIRECT=$(printf '%s\n' "$FILES" | grep -E '__tests__/.*\.(test|spec)\.[jt]sx?$'); """
    r"""SNAP=$(printf '%s\n' "$FILES" """
    r"""| grep -E '__tests__/__snapshots__/.*\.(test|spec)\.[jt]sx?\.snap$' """
    r"""| sed 's#__snapshots__/##; s#\.snap$##'); """
    r"""PAT=$(printf '%s\n%s\n' "$DIRECT" "$SNAP" | grep -v '^$' | sort -u """
    r"""| sed 's/$/$/' | paste -sd '|' -); """
    r"""./node_modules/.bin/jest --config .jest.js --no-cache --verbose --ci """
    r"""--testPathPattern "${PAT:-a^}" --passWithNoTests || true"""
)

_CHECK_GIT_CHANGES_SH = r"""#!/bin/bash
set -e

git rev-parse --is-inside-work-tree > /dev/null 2>&1
test -z "$(git status --porcelain)"
"""

_PREPARE_SH = r"""#!/bin/bash
set -e

cd /home/[[REPO]]

BASE_COMMIT="${BASE_COMMIT:-[[BASE_SHA]]}"

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach "${BASE_COMMIT}"
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"

ok=0
for attempt in 1 2 3 4; do
    if npm install --legacy-peer-deps --ignore-scripts --no-audit --no-fund; then
        ok=1
        break
    fi
    rm -rf node_modules 2>/dev/null || true
    sleep 15
done
test "$ok" -eq 1

if [ ! -f components/version/version.ts ]; then
  npm run version > /dev/null 2>&1 || true
fi
if [ ! -f components/version/version.ts ]; then
  node <<'VERSIONEOF'
const fs = require('fs');
const v = require(process.cwd() + '/package.json').version;
fs.writeFileSync('components/version/version.ts', "export default '" + v + "';\n");
VERSIONEOF
fi

CH=$(node -e "try{process.stdout.write(require('./node_modules/cheerio/package.json').version)}catch(e){process.stdout.write('none')}" 2>/dev/null || printf 'none')
case "$CH" in
  1.0.0-rc.*|none)
    ;;
  *)
    find node_modules -path "*/node_modules/cheerio" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null || true
    for attempt in 1 2 3; do
      if npm install --no-save --no-package-lock --legacy-peer-deps cheerio@1.0.0-rc.10 > /dev/null 2>&1 \
         && [ -f node_modules/cheerio/package.json ]; then
        break
      fi
      sleep 10
    done
    ;;
esac
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {} + 2>/dev/null || true

node <<'JESTCFGEOF' > /dev/null 2>&1 || true
const fs = require('fs');
try {
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {
    if (!c.includes("'" + m + "'")) {
      c = c.replace('const compileModules = [', "const compileModules = [\n  '" + m + "',");
      changed = true;
    }
  }
  if (changed) {
    fs.writeFileSync('.jest.js', c);
  }
} catch (e) {}
JESTCFGEOF

git add .jest.js

test -d node_modules
test -x node_modules/.bin/jest
test -f components/version/version.ts
grep -q "'@csstools'," .jest.js
grep -q "'@asamuzakjp/dom-selector'," .jest.js
grep -q "'@exodus'," .jest.js
git diff --quiet -- .jest.js
node -e "require('./package.json')"
node -e "require('./node_modules/@ant-design/tools/lib/jest/codePreprocessor.js')"
test "$(./node_modules/.bin/jest --config .jest.js --listTests 2>/dev/null | wc -l)" -gt 0
"""

_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO]] || exit 1
[[TEST_CMD]]
exit 0
"""

_PATCH_RUN_SH = r"""#!/bin/bash
set -o pipefail

cd /home/[[REPO]] || exit 1
git checkout -- . 2>/dev/null || true

if ! git apply --3way --whitespace=nowarn [[PATCHES]]; then
  echo "MSWEB_FATAL: could not apply [[PATCHES]]"
  echo "FAIL msweb/preflight.test.js"
  echo "  ✕ patch application failed"
  exit 1
fi

[[TEST_CMD]]
exit 0
"""


class _AntDesignImageBase_51297_to_46035(Image):
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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_IMAGE_TAG

    def workdir(self) -> str:
        return _BASE_IMAGE_TAG

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

RUN npm config set fetch-timeout 120000 2>/dev/null && \\
    npm config set fetch-retries 5 2>/dev/null && \\
    npm config set fetch-retry-mintimeout 10000 2>/dev/null && \\
    npm config set fetch-retry-maxtimeout 60000 2>/dev/null && \\
    npm config set maxsockets 3 2>/dev/null || true

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

CMD ["/bin/bash"]
"""


class _AntDesignImageDefault_51297_to_46035(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Optional[Image]:
        return _AntDesignImageBase_51297_to_46035(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str, **subs: str) -> str:
        out = template.replace("[[REPO]]", self.pr.repo)
        out = out.replace("[[TEST_CMD]]", TEST_CMD)
        for key, value in subs.items():
            out = out.replace(f"[[{key}]]", value)
        return out

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(
                ".",
                "prepare.sh",
                self._render(_PREPARE_SH, BASE_SHA=self.pr.base.sha),
            ),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(
                ".",
                "test-run.sh",
                self._render(_PATCH_RUN_SH, PATCHES="/home/test.patch"),
            ),
            File(
                ".",
                "fix-run.sh",
                self._render(
                    _PATCH_RUN_SH, PATCHES="/home/test.patch /home/fix.patch"
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if image is None:
            raise ValueError(
                "_AntDesignImageDefault_51297_to_46035 dependency must be an Image"
            )
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        hardening_block = r"""RUN set -eux; \
    git checkout --detach "${BASE_COMMIT}"; \
    git remote remove origin 2>/dev/null || true; \
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \
        | xargs -r -n1 git update-ref -d; \
    git reflog expire --expire=now --all; \
    git reflog expire --expire-unreachable=now --all; \
    git gc --prune=now --aggressive; \
    git repack -a -d -l --quiet; \
    rm -f .git/objects/info/alternates; \
    git config --local gc.auto 0; \
    git config --local fetch.recurseSubmodules false; \
    git config --local remote.pushDefault ""; \
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"; \
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \
    test -z "$(git remote)"; \
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \
        git submodule foreach --recursive ' \
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

        return rf"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

ENV NODE_ENV=test \
    CI=true \
    NODE_OPTIONS=--max-old-space-size=4096

WORKDIR /home/{repo}

{copy_commands}
RUN bash /home/prepare.sh

{hardening_block}
"""


@Instance.register("ant-design", "ant_design_51297_to_46035")
class AntDesign_51297_to_46035(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        if not (_PR_LOW <= pr.number <= _PR_HIGH):
            raise ValueError(
                f"ant-design PR {pr.number} is tagged ant_design_51297_to_46035 but falls "
                f"outside {_PR_LOW}-{_PR_HIGH}"
            )
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return _AntDesignImageDefault_51297_to_46035(self.pr, self._config)

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
        return parse_jest_log(test_log)
