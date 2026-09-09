from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log


class AntDesignImageBase_ANT_DESIGN_4756_TO_563(Image):
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
        return "node:6"

    def image_tag(self) -> str:
        return "base-4756_to_563"

    def workdir(self) -> str:
        return "base-4756_to_563"

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


class AntDesignImageDefault_ANT_DESIGN_4756_TO_563(Image):
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
        return AntDesignImageBase_ANT_DESIGN_4756_TO_563(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _extra_tail(self) -> str:
        """Runs at the END of prepare.sh, after every install step.

        The symlink must come after the script's own `typescript-babel-jest`
        install a few lines further down -- placing it with the other repairs
        made the guard test a directory that did not exist yet, so no symlink
        was created and jest failed to resolve the transform exactly as before.
        """
        if self.pr.number not in (3830, 4700):
            return ""
        return """
# --- per-PR repair tail (see _extra_tail) ---
if [ -d node_modules/typescript-babel-jest ]; then
  echo "linking typescript-babel-jest for jest package-style transform resolution"
  mkdir -p node_modules/node_modules
  ln -sfn /home/REPO_NAME/node_modules/typescript-babel-jest node_modules/node_modules/typescript-babel-jest
fi
node -e "console.log('react present:', require('fs').existsSync('node_modules/react'))"
""".replace("REPO_NAME", self.pr.repo)

    def _extra_setup(self) -> str:
        """Per-PR repairs for base commits where `npm install` ends early.

        At PRs 3830 and 4700 the install terminates with react and react-dom
        absent even though both are declared devDependencies, so every test file
        dies on `import React from 'react'` and jest reports zero tests. PR 4756
        -- same era, same script -- installs cleanly, so this is scoped by number
        rather than applied era-wide: that keeps 4756's prepare.sh byte-identical
        to the one that built its working image.

        The transform fix is separate: package.json sets the jest transform to
        "node_modules/typescript-babel-jest", and jest resolves a slash-path as a
        PACKAGE name, i.e. it searches <rootDir>/node_modules/node_modules/... .
        The symlink puts the module where that lookup actually looks. Nothing
        here touches a tracked file, so the clean-tree assert still holds.
        """
        if self.pr.number not in (3830, 4700):
            return ""
        return """
# --- per-PR repair (see _extra_setup) ---
# npm 3.10.10 (bundled with node:6) aborts with ENOTDIR while staging scoped
# packages -- observed on @types/mdast -- and rolls the whole install back, so
# node_modules ends up empty or partial. Measured on this image: npm 3 exits 236
# with 0 packages on every attempt; npm 6.14.18 exits 0 with 1787 packages.
# npm 3 cannot self-upgrade past the same bug (it deletes its own binary), so
# npm 6 is unpacked from its tarball directly. npm 6 still supports node 6.
curl -fsSL -o /tmp/npm6.tgz https://registry.npmjs.org/npm/-/npm-6.14.18.tgz \
  && tar -xzf /tmp/npm6.tgz -C /tmp \
  && rm -rf /usr/local/lib/node_modules/npm \
  && mv /tmp/package /usr/local/lib/node_modules/npm \
  && ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && echo "npm upgraded to $(npm --version) for the install below"
rm -rf node_modules
npm install --no-audit --no-fund
node -e "console.log('modules installed:', require('fs').readdirSync('node_modules').length)"
""".replace("REPO_NAME", self.pr.repo)

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
{extra}
# Ensure jest is available (some ancient PRs don't have it in devDependencies)
if [ ! -f ./node_modules/.bin/jest ]; then
  echo "jest not found, force-installing jest@18 + babel-jest@18 for compatibility with node:6"
  npm install --no-save jest@18 babel-jest@18 react-test-renderer@15 2>/dev/null || true
fi

# Install typescript-babel-jest if the jest config references it (needed for some PRs)
if grep -q "typescript-babel-jest" package.json 2>/dev/null; then
  echo "Found typescript-babel-jest reference, installing..."
  npm install --no-save typescript-babel-jest 2>/dev/null || true
fi

# Create stub tests/setup.js if it doesn't exist (some ancient PRs lack it)
if [ ! -f tests/setup.js ]; then
  echo "Creating stub tests/setup.js"
  mkdir -p tests
  echo "// stub setup" > tests/setup.js
fi

# Only install jsdom@11 if tests/setup.js does NOT use the old jsdom v9 API (import {{ jsdom }} from 'jsdom')
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
{extra_tail}""".format(repo=self.pr.repo, base_sha=self.pr.base.sha, extra=self._extra_setup(), extra_tail=self._extra_tail()),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
./node_modules/.bin/jest --verbose || true
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
./node_modules/.bin/jest --verbose || true
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
./node_modules/.bin/jest --verbose || true
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


@Instance.register("ant-design", "ant_design_4756_to_563")
class ANT_DESIGN_4756_TO_563(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_4756_TO_563(self.pr, self._config)

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
