import re
from typing import Optional, Union
import textwrap
from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

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

{self.clear_env}

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
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

NODE_SASS_VER=$(node -e "try {{ var p = require('./package.json'); var v = (p.devDependencies && p.devDependencies['node-sass']) || (p.dependencies && p.dependencies['node-sass']) || ''; console.log(v); }} catch(e) {{ console.log(''); }}" 2>/dev/null || echo "")

if [ -z "$NODE_SASS_VER" ]; then
  NODE_VER=18
else
  NODE_VER=16
fi

nvm install $NODE_VER || true
nvm use $NODE_VER || true
nvm alias default $NODE_VER
echo "Using Node.js $(node --version) for node-sass=$NODE_SASS_VER"

NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

which python || ln -sf $(which python3) /usr/local/bin/python

npm install --ignore-scripts --legacy-peer-deps || true

if [ -n "$NODE_SASS_VER" ]; then
  npm install sass@1.35.2 --legacy-peer-deps --no-save 2>/dev/null || true
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
  echo "Installed dart-sass shim as node-sass"
fi

npm rebuild --legacy-peer-deps 2>/dev/null || true

if [ -n "$NODE_SASS_VER" ]; then
  npm install sass@1.35.2 --ignore-scripts --legacy-peer-deps --no-save 2>/dev/null || true
fi

WEBPACK_VER=$(node -e "try {{ console.log(require('webpack/package.json').version.split('.')[0]); }} catch(e) {{ console.log('0'); }}" 2>/dev/null || echo "0")
if [ "$WEBPACK_VER" = "3" ]; then
  npm install cheerio@1.0.0-rc.3 --ignore-scripts --legacy-peer-deps --no-save 2>/dev/null || true
  echo "Pinned cheerio to 1.0.0-rc.3 for webpack 3"
fi

if [ -n "$NODE_SASS_VER" ]; then
  if ! node -e "require('sass')" 2>/dev/null; then
    SASS_TMP=$(mktemp -d)
    npm pack sass@1.35.2 --pack-destination "$SASS_TMP" 2>/dev/null || true
    rm -rf node_modules/sass
    mkdir -p node_modules/sass
    tar -xzf "$SASS_TMP"/sass-*.tgz -C node_modules/sass --strip-components=1 2>/dev/null || true
    rm -rf "$SASS_TMP"
    echo "Re-installed sass via npm pack (was removed by cheerio install)"
  fi
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
  echo "Applied dart-sass shim as node-sass"
fi

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
  echo "Applied dart-sass shim as node-sass (final step)"
fi

if [ -f scripts/test/karma.js ]; then
  sed -i "s/flags: \\['--no-sandbox'\\]/flags: ['--no-sandbox', '--disable-gpu', '--disable-software-rasterizer', '--disable-dev-shm-usage']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/options.browsers = \\['ChromeHeadless'\\]/options.browsers = ['ChromeTravis']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s/options.browsers = \\['ChromeTravis'\\]/options.browsers = ['ChromeTravis']/" scripts/test/karma.js 2>/dev/null || true
  sed -i "s|process.env.CHROME_BIN = require('puppeteer').executablePath()|process.env.CHROME_BIN = '/usr/bin/chromium'|" scripts/test/karma.js 2>/dev/null || true
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
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

npm run test || true
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export CI=true
export TRAVIS=true
export CHROME_BIN=$(which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null || which chromium 2>/dev/null || echo "")
export ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || true

nvm use default || true
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

if [ -f tools/test.ts ]; then
  sed -i "s/-b.*chrome'/-b', 'chromium'/g" tools/test.ts 2>/dev/null || true
fi

npm run test || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
export CI=true
export TRAVIS=true
export CHROME_BIN=$(which google-chrome-stable 2>/dev/null || which google-chrome 2>/dev/null || which chromium 2>/dev/null || echo "")
export ELECTRON_EXTRA_LAUNCH_ARGS="--disable-gpu --disable-software-rasterizer"
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || true

nvm use default || true
NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])")
if [ "$NODE_MAJOR" -ge 17 ] 2>/dev/null; then
  export NODE_OPTIONS=--openssl-legacy-provider
fi

if [ -f tools/test.ts ]; then
  sed -i "s/-b.*chrome'/-b', 'chromium'/g" tools/test.ts 2>/dev/null || true
fi

npm run test || true

""".format(pr=self.pr),
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

{self.global_env}

{proxy_setup}

{copy_commands}

{prepare_commands}

{proxy_cleanup}

{self.clear_env}

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
            # 如果是统计行，提取文件名
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
        ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
        for line in test_log.splitlines():
            line = ansi_escape.sub("", line)
            line = line.strip()
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

        for test in failed_tests:
            if test in ignore_tests:
                failed_tests.remove(test)

        if failed_tests:
            failed_tests.add("ToTal_Test")
        else:
            passed_tests.add("ToTal_Test")

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
