import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.repos.typescript.ant_design.ant_design import parse_jest_log



def _test_scope(pr: PullRequest) -> str:
    paths = []
    for line in (pr.test_patch or "").split(chr(10)):
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if not m:
            continue
        path = m.group(2).strip()
        if not re.search(r"[.](test|spec)[.](ts|tsx|js|jsx)$", path):
            continue
        if path not in paths:
            paths.append(path)
    return " ".join(paths)

class AntDesignImageBase_ANT_DESIGN_53250_TO_51425(Image):
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
        return "node:20"

    def image_tag(self) -> str:
        return "base-53250_to_51425"

    def workdir(self) -> str:
        return "base-53250_to_51425"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = (
                f"RUN git clone https://github.com/"
                f"{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \
      org.opencontainers.image.description="{org}/{repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

{self.global_env}

WORKDIR /home/

{code}

WORKDIR /home/{repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class AntDesignImageDefault_ANT_DESIGN_53250_TO_51425(Image):
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
        return AntDesignImageBase_ANT_DESIGN_53250_TO_51425(self.pr, self.config)

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
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{base_sha}"

for attempt in 1 2 3 4; do
    npm install --legacy-peer-deps --ignore-scripts && break
    echo "prepare: npm install attempt $attempt failed; retrying in 15s"
    sleep 15
done

find node_modules -path "*/node_modules/cheerio" -type d -exec rm -rf {{}} + 2>/dev/null
rm -rf node_modules/cheerio node_modules/.package-lock.json 2>/dev/null
for attempt in 1 2 3; do
    npm install --no-save --legacy-peer-deps --ignore-scripts cheerio@1.0.0-rc.10 2>/dev/null && break
    echo "prepare: cheerio pin attempt $attempt failed; retrying in 10s"
    sleep 10
done
for nested in $(find node_modules -path "*/node_modules/cheerio/dist" -type d 2>/dev/null); do
  nested_dir=$(dirname "$nested")
  if [ -d node_modules/cheerio ]; then
    rm -rf "$nested_dir"
    cp -r node_modules/cheerio "$nested_dir" || true
  else
    echo "prepare: WARNING cheerio pin unavailable; leaving $nested_dir as installed"
  fi
done
find node_modules -name "parse5-parser-stream" -type d -exec rm -rf {{}} + 2>/dev/null
npm run version || true

node << 'PATCHEOF' || true
const fs = require('fs');
try {{
  let c = fs.readFileSync('.jest.js', 'utf8');
  const needed = ['@exodus', 'jsdom', '@csstools', '@asamuzakjp/dom-selector'];
  let changed = false;
  for (const m of needed) {{
    if (!c.includes("'" + m + "'")) {{
      c = c.replace('const compileModules = [', "const compileModules = [\\n  '" + m + "',");
      changed = true;
    }}
  }}
  if (changed) {{ fs.writeFileSync('.jest.js', c); console.log('Patched .jest.js ESM modules'); }}
}} catch(e) {{ console.log('No .jest.js to patch'); }}
PATCHEOF
""".format(repo=self.pr.repo, base_sha=self.pr.base.sha),
            ),
            File(
                ".",
                "run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
npx jest --config .jest.js --no-cache --verbose {scope} || true
""".format(repo=self.pr.repo, scope=_test_scope(self.pr)),
            ),
            File(
                ".",
                "test-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
if ! git diff --quiet HEAD -- package.json; then
    npm install --legacy-peer-deps --ignore-scripts || true
fi
npx jest --config .jest.js --no-cache --verbose {scope} || true
""".format(repo=self.pr.repo, scope=_test_scope(self.pr)),
            ),
            File(
                ".",
                "fix-run.sh",
                """\
#!/bin/bash
set -eo pipefail

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
if ! git diff --quiet HEAD -- package.json; then
    npm install --legacy-peer-deps --ignore-scripts || true
fi
npx jest --config .jest.js --no-cache --verbose {scope} || true
""".format(repo=self.pr.repo, scope=_test_scope(self.pr)),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("AntDesignImageDefault_ANT_DESIGN_53250_TO_51425 dependency must be an Image")
        name = image.image_name()
        tag = image.image_tag()

        graded = {"run.sh", "test-run.sh", "fix-run.sh"}
        copy_commands = ""
        copy_after = ""
        for file in self.files():
            line = f"COPY {file.name} /home/\n"
            if file.name in graded:
                copy_after += line
            else:
                copy_commands += line

        sha = self.pr.base.sha
        hardening = (
            Image._HARDENING_BLOCK
            .replace(" --aggressive", "")
            .replace('"${BASE_COMMIT}"', sha)
        )
        pack_limits = (
            "RUN git config --local pack.windowMemory 256m && \
"
            "    git config --local pack.deltaCacheSize 128m && \
"
            "    git config --local pack.threads 2"
        )

        useid_shim = r'''RUN set -eux; \
    d=node_modules/@rc-component/select/lib/hooks; \
    if [ -d node_modules/@rc-component/cascader ] \
       && [ ! -f "$d/useId.js" ] \
       && [ -f node_modules/@rc-component/util/lib/hooks/useId.js ]; then \
        mkdir -p "$d"; \
        printf "module.exports = require('@rc-component/util/lib/hooks/useId');\n" > "$d/useId.js"; \
    fi'''

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

WORKDIR /home/{self.pr.repo}

{pack_limits}

{hardening}
RUN bash /home/prepare.sh

{useid_shim}

{copy_after}

{self.clear_env}

"""


@Instance.register("ant-design", "ant_design_53250_to_51425")
class ANT_DESIGN_53250_TO_51425(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return AntDesignImageDefault_ANT_DESIGN_53250_TO_51425(self.pr, self._config)

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
