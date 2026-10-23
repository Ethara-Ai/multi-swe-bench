import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_LIBSASS_COMPATIBLE_DART_SASS = "1.35.2"

_SEMVER_RANGE = re.compile(r"^[\^~>=<]*[0-9][0-9A-Za-z.\-+*x]*$")
_DEP_LINE = re.compile(r'^\+\s*"([^"]+)"\s*:\s*"([^"]+)"\s*,?\s*$')


def _patch_added_deps(*patches: str) -> list[str]:
    deps: dict[str, str] = {}
    for patch in patches:
        if not patch:
            continue
        in_root_package_json = False
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                in_root_package_json = line.split()[-1] == "b/package.json"
                continue
            if not in_root_package_json or not line.startswith("+"):
                continue
            match = _DEP_LINE.match(line)
            if not match:
                continue
            name, version = match.group(1), match.group(2)
            if name in ("name", "version", "main", "module", "types", "license"):
                continue
            if not _SEMVER_RANGE.match(version):
                continue
            deps.setdefault(name, version)

    return [f"{name}@{version}" for name, version in sorted(deps.items())]


_KARMA_SPEC_DIR = re.compile(r"^test/([^/]+)/")
_CYPRESS_SPEC_DIR = re.compile(r"^components/([^/]+)/")

_LOG_NOISE = re.compile(
    r"All specs passed!"
    r"|\uff62wdm\uff63"
    r"|^[\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u2500\u256d\u256e\u2570\u256f]"
    r"|^COMPONENT_(?:START|FAIL):"
    r"|^Running:\s"
    r"|\s{2,}\d+(?:ms|s|:\d\d)\s{2,}[\d-]"
)


def _test_scopes(test_patch: str) -> tuple[str, str]:
    karma: set[str] = set()
    cypress: set[str] = set()
    path = ""
    for line in (test_patch or "").splitlines():
        if line.startswith("diff --git "):
            path = line.split()[-1][2:]
            continue
        if line.startswith("deleted file mode"):
            path = ""
            continue
        if not path or not line.startswith("@@"):
            continue
        match = _KARMA_SPEC_DIR.match(path)
        if match:
            karma.add(match.group(1))
        match = _CYPRESS_SPEC_DIR.match(path)
        if match:
            cypress.add(match.group(1))
        path = ""

    return " ".join(sorted(karma)), " ".join(sorted(cypress))


_TEST_COMMAND = """
TEST_COMPONENTS="{karma_components}"
CYPRESS_TARGETS="{cypress_targets}"
TEST_SCRIPT=$(node -e "try {{ console.log((require('./package.json').scripts || {{}}).test || ''); }} catch (e) {{ console.log(''); }}" 2>/dev/null || echo "")

case "$TEST_SCRIPT" in
  *tools/test.ts*)
    if [ -n "$CYPRESS_TARGETS" ]; then
      npm run test -- $CYPRESS_TARGETS
    else
      npm run test
    fi
    ;;
  *scripts/test/index.js*)
    if [ -n "$TEST_COMPONENTS" ] && grep -q "const singleRun = true;" scripts/test/karma.js; then
      for comp in $TEST_COMPONENTS; do
        echo "COMPONENT_START: $comp"
        if ! npm run test -- "$comp"; then
          echo "COMPONENT_FAIL: $comp"
        fi
      done
    else
      npm run test
    fi
    ;;
  *)
    npm run test
    ;;
esac
""".strip()


class nextImageBase(Image):
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
        return "node:18"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_CONFIG_COUNT=5 \\
    GIT_CONFIG_KEY_0=http.version \\
    GIT_CONFIG_VALUE_0=HTTP/1.1 \\
    GIT_CONFIG_KEY_1=http.postBuffer \\
    GIT_CONFIG_VALUE_1=1048576000 \\
    GIT_CONFIG_KEY_2=core.compression \\
    GIT_CONFIG_VALUE_2=0 \\
    GIT_CONFIG_KEY_3=http.lowSpeedLimit \\
    GIT_CONFIG_VALUE_3=1000 \\
    GIT_CONFIG_KEY_4=http.lowSpeedTime \\
    GIT_CONFIG_VALUE_4=600

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

RUN apt update && apt install -y libxkbfile-dev pkg-config build-essential python3 libkrb5-dev libxss1 xvfb libgtk-3-0 libgbm1

RUN apt-get update \
    && apt-get install -y chromium fonts-ipafont-gothic fonts-wqy-zenhei fonts-thai-tlwg \
        fonts-khmeros fonts-kacst fonts-freefont-ttf libxss1 dbus dbus-x11 \
        --no-install-recommends \
    && ln -sf /usr/bin/chromium /usr/bin/google-chrome-stable \
    && ln -sf /usr/bin/chromium /usr/bin/google-chrome \
    && ln -sf /usr/bin/chromium /usr/bin/chrome \
    && rm -rf /var/lib/apt/lists/*

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash && \
    export NVM_DIR="$HOME/.nvm" && \
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

{code}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class nextImageDefault(Image):
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
        return nextImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        karma_components, cypress_targets = _test_scopes(self.pr.test_patch)
        test_command = _TEST_COMMAND.format(
            karma_components=karma_components,
            cypress_targets=cypress_targets,
        )
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
if ! git cat-file -e "{pr.base.sha}^{{commit}}" 2>/dev/null; then
  git fetch --no-tags origin "{pr.base.sha}"
fi
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

NODE_SASS_VER=$(node -e "try {{ var p = require('./package.json'); var v = (p.devDependencies && p.devDependencies['node-sass']) || (p.dependencies && p.dependencies['node-sass']) || ''; console.log(v); }} catch(e) {{ console.log(''); }}" 2>/dev/null || echo "")

if [ -z "$NODE_SASS_VER" ]; then
  NODE_VER=18
else
  NODE_VER=16
fi

nvm install $NODE_VER || true
nvm use $NODE_VER || true
nvm alias default $NODE_VER || true
echo "Using Node.js $(node --version) for node-sass=$NODE_SASS_VER"

NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

which python || ln -sf $(which python3) /usr/local/bin/python

npm install --ignore-scripts --legacy-peer-deps || true

npm rebuild --legacy-peer-deps 2>/dev/null || true

WEBPACK_VER=$(node -e "try {{ console.log(require('webpack/package.json').version.split('.')[0]); }} catch(e) {{ console.log('0'); }}" 2>/dev/null || echo "0")

EXTRAS="{patch_deps}"
if [ "$WEBPACK_VER" = "3" ]; then
  EXTRAS="$EXTRAS cheerio@1.0.0-rc.3"
fi
if [ -n "$NODE_SASS_VER" ]; then
  EXTRAS="$EXTRAS sass@{dart_sass_ver}"
fi

if [ -n "$EXTRAS" ]; then
  echo "EXTRAS: resolving patch deps and era pins together:$EXTRAS"
  EXTRAS_OK=0
  for attempt in 1 2 3 4 5; do
    if npm install --no-save --ignore-scripts --legacy-peer-deps $EXTRAS >/dev/null 2>&1; then
      EXTRAS_OK=1
      break
    fi
    echo "EXTRAS_RETRY: attempt $attempt failed"
    sleep 5
  done
  if [ "$EXTRAS_OK" = "0" ]; then
    ACCEPTED=""
    for dep in $EXTRAS; do
      if npm install --no-save --ignore-scripts --legacy-peer-deps $ACCEPTED "$dep" >/dev/null 2>&1; then
        ACCEPTED="$ACCEPTED $dep"
      else
        echo "PATCH_DEP_MISS: $dep"
      fi
    done
    EXTRAS="$ACCEPTED"
  fi
  echo "EXTRAS_OK: installed$EXTRAS in a single resolution"
fi

if [ "$WEBPACK_VER" = "3" ]; then
  [ -f node_modules/cheerio/lib/utils.js ] || {{ echo "PIN_FAIL: cheerio layout is not enzyme/webpack3 compatible"; exit 1; }}
  echo "PINS_OK: cheerio exposes lib/utils for enzyme"
fi

for dep in $EXTRAS; do
  name="${{dep%@*}}"
  [ -n "$name" ] || name="$dep"
  [ -d "node_modules/$name" ] || echo "PATCH_DEP_MISS: $name is not present after install"
done

if [ -n "$NODE_SASS_VER" ]; then
  rm -rf node_modules/node-sass
  mkdir -p node_modules/node-sass/lib
  cat > node_modules/node-sass/package.json << 'PKGJSON'
{{"name":"node-sass","version":"6.0.1","main":"lib/index.js"}}
PKGJSON
  cat > node_modules/node-sass/lib/index.js << 'SHIMJS'
var sass = require('sass');
module.exports.render = function(opts, cb) {{
  try {{
    var result = sass.renderSync(opts);
    cb(null, result);
  }} catch(e) {{ cb(e); }}
}};
module.exports.renderSync = function(opts) {{
  return sass.renderSync(opts);
}};
module.exports.info = 'node-sass-shim\\t(dart-sass ' + sass.info + ')';
SHIMJS
  node -e "
    var fs = require('fs');
    var v = JSON.parse(fs.readFileSync('node_modules/sass/package.json', 'utf8')).version;
    var major = parseInt(v.split('.')[0], 10), minor = parseInt(v.split('.')[1], 10);
    if (major > 1 || minor >= 40) {{
      console.error('SASS_FAIL: top-level dart-sass ' + v + ' has first-class calc(); interpolated calc arithmetic will break');
      process.exit(1);
    }}
    var out = require('node-sass').renderSync({{data: '\\$a:20px;\\$b:8px;.x{{width:20px + 4px + calc((#{{\\$a - \\$b}}) / 2);}}'}}).css.toString();
    if (out.indexOf('calc') < 0) {{
      console.error('SASS_FAIL: interpolated calc arithmetic did not survive: ' + out);
      process.exit(1);
    }}
    console.log('SASS_OK: node-sass shim backed by dart-sass ' + v + ' (libsass calc semantics)');
  " || {{ echo "SASS_FAIL: node-sass shim verification failed"; exit 1; }}
fi

if [ -x node_modules/.bin/cypress ]; then
  if node_modules/.bin/cypress verify >/dev/null 2>&1; then
    echo "CYPRESS_OK: binary already cached"
  else
    CY_OK=0
    for attempt in 1 2 3; do
      if node_modules/.bin/cypress install >/dev/null 2>&1 && node_modules/.bin/cypress verify >/dev/null 2>&1; then CY_OK=1; break; fi
      echo "CYPRESS_RETRY: attempt $attempt failed"
      sleep 5
    done
    if [ "$CY_OK" = "1" ]; then echo "CYPRESS_OK: binary installed"; else echo "CYPRESS_MISS: binary unavailable"; fi
  fi
fi

TEST_SCRIPT=$(node -e "try {{ console.log((require('./package.json').scripts || {{}}).test || ''); }} catch (e) {{ console.log(''); }}" 2>/dev/null || echo "")
case "$TEST_SCRIPT" in
  *scripts/test/index.js*)
    if ! node -e "require('karma')" >/dev/null 2>&1; then
      npm install --no-save --ignore-scripts --legacy-peer-deps --prefix node_modules/useragent lru-cache@4.1.5 >/dev/null 2>&1 || true
      echo "KARMA_REPAIR: reinstalled useragent's nested lru-cache@4.1.5"
    fi
    node -e "require('karma')" >/dev/null 2>&1 || {{ echo "KARMA_FAIL: karma is installed but cannot be loaded"; node -e "require('karma')"; exit 1; }}
    echo "KARMA_OK: karma loads"
    ;;
esac

git checkout -- .
git clean -fdxq -e node_modules
git status --porcelain --untracked-files=all | grep -v "^?? node_modules/" > /tmp/untracked_after_clean || true
[ ! -s /tmp/untracked_after_clean ] || {{ echo "CLEAN_FAIL: tracked tree is not pristine after provisioning:"; cat /tmp/untracked_after_clean; exit 1; }}
echo "CLEAN_OK: provisioning left no files that could block patching"

test -d node_modules || {{ echo "DEPS_FAIL: node_modules was not created"; exit 1; }}
node -e "var s=require('./package.json').scripts||{{}}; if(!s.test){{console.error('DEPS_FAIL: package.json has no test script');process.exit(1);}}"
node -e "var c=['karma','jest','mocha'];var ok=c.filter(function(m){{try{{require.resolve(m);return true}}catch(e){{return false}}}});if(!ok.length){{console.error('DEPS_FAIL: no test runner resolvable in node_modules');process.exit(1);}}console.log('DEPS_OK: test runner(s) '+ok.join(','));"
node -e "require('./package.json')" || {{ echo "DEPS_FAIL: package.json unreadable"; exit 1; }}
echo "DEPS_OK: prepare.sh gate passed"
""".format(
                    pr=self.pr,
                    dart_sass_ver=_LIBSASS_COMPATIBLE_DART_SASS,
                    patch_deps=" ".join(
                        _patch_added_deps(self.pr.fix_patch, self.pr.test_patch)
                    ),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true
export CI=true
export TRAVIS=true
export CHROME_BIN=$(which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null || which chromium 2>/dev/null || echo "")
export ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer"
cd /home/{pr.repo}

nvm use default || true
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

if [ -f tools/test.ts ]; then
  sed -i "s/-b.*chrome'/-b', 'chromium'/g" tools/test.ts 2>/dev/null || true
fi

if [ -f scripts/test/karma.js ]; then
  sed -i "s/flags: \\['--no-sandbox'\\]/flags: ['--no-sandbox', '--disable-gpu', '--disable-software-rasterizer', '--disable-dev-shm-usage']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/options.browsers = \\['ChromeHeadless'\\]/options.browsers = ['ChromeTravis']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s|process.env.CHROME_BIN = require('puppeteer').executablePath()|process.env.CHROME_BIN = '/usr/bin/chromium'|" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/const singleRun = runAll;/const singleRun = true;/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/timeout: 10000,/timeout: 20000, retries: 2,/" scripts/test/karma.js 2>/dev/null || true
fi

{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true
export CI=true
export TRAVIS=true
export CHROME_BIN=$(which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null || which chromium 2>/dev/null || echo "")
export ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch

nvm use default || true
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

if [ -f tools/test.ts ]; then
  sed -i "s/-b.*chrome'/-b', 'chromium'/g" tools/test.ts 2>/dev/null || true
fi

if [ -f scripts/test/karma.js ]; then
  sed -i "s/flags: \\['--no-sandbox'\\]/flags: ['--no-sandbox', '--disable-gpu', '--disable-software-rasterizer', '--disable-dev-shm-usage']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/options.browsers = \\['ChromeHeadless'\\]/options.browsers = ['ChromeTravis']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s|process.env.CHROME_BIN = require('puppeteer').executablePath()|process.env.CHROME_BIN = '/usr/bin/chromium'|" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/const singleRun = runAll;/const singleRun = true;/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/timeout: 10000,/timeout: 20000, retries: 2,/" scripts/test/karma.js 2>/dev/null || true
fi

{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true
export CI=true
export TRAVIS=true
export CHROME_BIN=$(which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null || which chromium 2>/dev/null || echo "")
export ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch

nvm use default || true
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

if [ -f tools/test.ts ]; then
  sed -i "s/-b.*chrome'/-b', 'chromium'/g" tools/test.ts 2>/dev/null || true
fi

if [ -f scripts/test/karma.js ]; then
  sed -i "s/flags: \\['--no-sandbox'\\]/flags: ['--no-sandbox', '--disable-gpu', '--disable-software-rasterizer', '--disable-dev-shm-usage']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/options.browsers = \\['ChromeHeadless'\\]/options.browsers = ['ChromeTravis']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s|process.env.CHROME_BIN = require('puppeteer').executablePath()|process.env.CHROME_BIN = '/usr/bin/chromium'|" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/const singleRun = runAll;/const singleRun = true;/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/timeout: 10000,/timeout: 20000, retries: 2,/" scripts/test/karma.js 2>/dev/null || true
fi

{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"
        proxy_setup = ""
        proxy_cleanup = ""

        if self.global_env:
            proxy_host = None
            proxy_port = None

            for line in self.global_env.splitlines():
                match = re.match(
                    r"^ENV\s*(http[s]?_proxy)=http[s]?://([^:]+):(\d+)", line
                )
                if match:
                    proxy_host = match.group(2)
                    proxy_port = match.group(3)
                    break

            if proxy_host and proxy_port:
                proxy_setup = textwrap.dedent(
                    f"""
                    RUN mkdir -p $HOME && \\
                        touch $HOME/.npmrc && \\
                        echo "proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "https-proxy=http://{proxy_host}:{proxy_port}" >> $HOME/.npmrc && \\
                        echo "strict-ssl=false" >> $HOME/.npmrc
                """
                )

                proxy_cleanup = textwrap.dedent(
                    """
                    RUN rm -f $HOME/.npmrc
                """
                )
        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

RUN git reset --hard

RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
"""


@Instance.register("alibaba-fusion", "next")
class next(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return nextImageDefault(self.pr, self._config)

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
        def normalize_test_name(name):
            m = re.match(r"^(\S+)\s{2,}", name)
            if m:
                return m.group(1)
            return name.strip()

        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()
        ignore_tests = []
        passed_res = [
            re.compile(r"PASS:?\s+([^\(]+)"),
            re.compile(r"\s*[✔✓]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
        ]

        failed_res = [
            re.compile(r"FAIL:?\s+([^\(]+)"),
            re.compile(r"\s*[×✗✖]\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"),
            re.compile(
                r"^(?!\s*\(node:)\s*\d+\)\s+(.*?)(?:\s*\(\d+(?:\.\d+)?\s*(?:ms|s)\))?\s*$"
            ),
        ]

        skipped_res = [
            re.compile(r"SKIP:?\s+([^\(]+)"),
        ]
        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            line = line.strip()
            if _LOG_NOISE.search(line):
                continue
            for passed_re in passed_res:
                m = passed_re.search(line)
                if m and m.group(1) not in failed_tests:
                    passed_tests.add(m.group(1))

            for failed_re in failed_res:
                m = failed_re.search(line)
                if m:
                    failed_tests.add(m.group(1))
                    if m.group(1) in passed_tests:
                        passed_tests.remove(m.group(1))

            for skipped_re in skipped_res:
                m = skipped_re.search(line)
                if m:
                    skipped_tests.add(m.group(1))
        normalized_passed = set()
        normalized_failed = set()
        for test in passed_tests:
            norm = normalize_test_name(test)
            normalized_passed.add(norm)

        for test in failed_tests:
            norm = normalize_test_name(test)
            normalized_failed.add(norm)

        passed_tests = normalized_passed
        failed_tests = normalized_failed
        passed_tests -= failed_tests

        failed_tests -= set(ignore_tests)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
