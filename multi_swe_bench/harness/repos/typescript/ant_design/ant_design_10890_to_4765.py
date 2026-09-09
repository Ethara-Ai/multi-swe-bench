from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log


class AntDesignImageBase_ANT_DESIGN_10890_TO_4765(Image):
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
        return "node:8"

    def image_tag(self) -> str:
        return "base-10890_to_4765"

    def workdir(self) -> str:
        return "base-10890_to_4765"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Environment and clone only.

        Emits the BuildKit syntax directive itself, which makes
        DockerfileEnhancer.enhance() return this file untouched (image.py:317).
        That is deliberate -- it is what keeps the injected checkout and history
        scrub out of the base -- and it is why the ARGs, ENV block, OCI labels
        and CA-certificate symlink farm are written here rather than inherited.

        The symlink farm precedes every network RUN, so the first HTTPS call
        already trusts the proxy CA. BASE_COMMIT is declared because the harness
        passes it, but deliberately unused: pinning happens in the PR layer.
        """
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
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

{code}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_10890_TO_4765(Image):
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
        return AntDesignImageBase_ANT_DESIGN_10890_TO_4765(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """\
#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {base_sha}
bash /home/check_git_changes.sh

npm install --legacy-peer-deps || true

if grep -q "from 'jsdom'" tests/setup.js 2>/dev/null || grep -q 'from "jsdom"' tests/setup.js 2>/dev/null; then
  echo "Detected old jsdom v9 API in tests/setup.js, skipping jsdom@11 pin"
  npm install --no-save nwsapi@2.2.2 2>/dev/null || true
else
  npm install --no-save --legacy-peer-deps jsdom@11.12.0 jest-environment-jsdom@24.9.0 nwsapi@2.2.2 2>/dev/null || true
fi

# Force cheerio@0.22.0 LAST — npm install above re-resolves enzyme's cheerio@^1.0.0-rc.2 to 1.x
find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {{}} + 2>/dev/null
rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null
npm install --no-save --legacy-peer-deps cheerio@0.22.0 2>/dev/null || true
for nested in $(find node_modules -path "*/node_modules/cheerio/dist" -type d 2>/dev/null); do
  nested_dir=$(dirname "$nested")
  rm -rf "$nested_dir"
  cp -r node_modules/cheerio "$nested_dir"
done
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {{}} + 2>/dev/null
CHEERIO_VER=$(node -e "try{{console.log(require('cheerio/package.json').version)}}catch(e){{console.log('none')}}" 2>/dev/null)
echo "cheerio version after pin: $CHEERIO_VER"

npm run version || true

# Detect jest version capability: jest >=22 supports JS config, <22 only JSON
JEST_MAJOR=$(node -e "try{{var v=require('./node_modules/jest/package.json').version;console.log(v.split('.')[0])}}catch(e){{console.log('0')}}" 2>/dev/null)

if [ -f .jest.js ]; then
  # .jest.js already exists natively — patch it
  node << 'PATCHEOF'
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  let changed = false;

  if (!c.includes('testURL')) {{
    c = c.replace(
      /module\\.exports\\s*=\\s*\\{{/,
      "module.exports = {{\\n  testURL: 'http://localhost',"
    );
    changed = true;
  }}

  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  for (const m of needed) {{
    if (c.includes('compileModules') && !c.includes("'" + m + "'")) {{
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }}
  }}

  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js'); }}
}} catch(e) {{ console.log('Error patching .jest.js: ' + e.message); }}
PATCHEOF
elif [ "$JEST_MAJOR" -ge 22 ] 2>/dev/null; then
  # jest >=22 supports JS config — create .jest.js
  echo "No .jest.js found, creating minimal jest config for jest >=$JEST_MAJOR"
  cat > .jest.js << 'JESTEOF'
const libDir = process.env.LIB_DIR || 'components';
module.exports = {{
  testURL: 'http://localhost',
  setupFiles: ['./tests/setup.js'],
  testPathIgnorePatterns: ['/node_modules/', 'dekko', 'node_modules'],
  transform: {{ '.*': './node_modules/babel-jest' }},
  collectCoverageFrom: [
    'components/**/*.{{js,jsx}}',
  ],
}};
JESTEOF
else
  # jest <22 can only read JSON config — inject testURL into package.json jest block
  echo "Old jest (v$JEST_MAJOR), injecting testURL into package.json"
  node << 'PKGEOF'
const fs = require('fs');
try {{
  const pkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
  if (pkg.jest && !pkg.jest.testURL) {{
    pkg.jest.testURL = 'http://localhost';
    fs.writeFileSync('package.json', JSON.stringify(pkg, null, 2));
    console.log('Injected testURL into package.json jest config');
  }}
}} catch(e) {{ console.log('Error patching package.json: ' + e.message); }}
PKGEOF
fi
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if [ -f .jest.js ]; then
  npx jest --config .jest.js --verbose || true
else
  npx jest --verbose || true
fi
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        """COPY, pin to the base commit, scrub history, then install deps.

        This file is never touched by DockerfileEnhancer: enhance() returns the
        raw text whenever dependency() is not a string (image.py:315), and a PR
        image depends on the base Image. The same rule means the PR build gets no
        BASE_COMMIT build-arg, which is why the SHA below is a literal taken from
        self.pr.base.sha rather than ${{BASE_COMMIT}}.

        The four `test` lines are the point of the scrub: HEAD is the base
        commit, no refs survive, no remote survives, and no unreachable history
        survives. Without them a mispinned or leaky image ships silently.
        """
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {self.pr.base.sha}; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh

{self.clear_env}
"""


@Instance.register("ant-design", "ant_design_10890_to_4765")
class ANT_DESIGN_10890_TO_4765(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_10890_TO_4765(self.pr, self._config)

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
