import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
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

ENV NODE_OPTIONS=--openssl-legacy-provider
ENV PUPPETEER_SKIP_DOWNLOAD=1

RUN apt update && apt install -y git chromium
RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-gpu --headless "$@"\\n' > /usr/local/bin/chromium-no-sandbox && chmod +x /usr/local/bin/chromium-no-sandbox
ENV CHROME_BIN=/usr/local/bin/chromium-no-sandbox
ENV CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox

WORKDIR /home/

{code}

RUN npm install -g pnpm@8
RUN apt install -y jq

{self.clear_env}

"""


class ImageBase12455(Image):
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
        return "base12455"

    def workdir(self) -> str:
        return "base12455"

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

ENV NODE_OPTIONS=--openssl-legacy-provider

WORKDIR /home/

{code}
RUN apt update && apt install -y git chromium
RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-gpu --headless "$@"\\n' > /usr/local/bin/chromium-no-sandbox && chmod +x /usr/local/bin/chromium-no-sandbox
ENV CHROME_BIN=/usr/local/bin/chromium-no-sandbox
ENV CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox
RUN apt install -y jq

{self.clear_env}

"""


class ImageDefault(Image):
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
        return ImageBase(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Detect test runner: vitest or karma
TEST_UNIT=$(node -e "var p=require('./package.json'); console.log(p.scripts && p.scripts['test:unit'] || '')" 2>/dev/null)
echo "test:unit script: $TEST_UNIT"

if echo "$TEST_UNIT" | grep -q "vitest"; then
  echo "VITEST detected - setting up vitest runner"
  echo "vitest" > /home/.test-runner

  pnpm install --no-frozen-lockfile 2>/dev/null || pnpm install 2>/dev/null || true

  # Create vitest result parser script
  cat > /home/parse-vitest-json.js << 'PARSEOF'
var fs = require("fs");
try {{
  var d = JSON.parse(fs.readFileSync("/tmp/vitest-results.json","utf8"));
  (d.testResults || []).forEach(function(f) {{
    (f.assertionResults || []).forEach(function(a) {{
      var name = a.fullName || (a.ancestorTitles || []).concat(a.title || []).join(" ");
      if (!name || name === "undefined") return;
      var status = a.status === "passed" ? "PASS" : (a.status === "failed" ? "FAIL" : "SKIP");
      console.log(status + ": " + name);
    }});
  }});
}} catch(e) {{
  console.error("Failed to parse vitest JSON: " + e.message);
}}
PARSEOF

else
  echo "KARMA detected - setting up karma runner"
  echo "karma" > /home/.test-runner

  pnpm install --ignore-scripts 2>/dev/null || pnpm install --ignore-scripts --no-frozen-lockfile 2>/dev/null || true

  rm -rf node_modules/karma-phantomjs-launcher node_modules/karma-safari-launcher node_modules/karma-firefox-launcher node_modules/karma-ie-launcher 2>/dev/null || true

  mkdir -p node_modules/karma-inline-spec-reporter
  cat > node_modules/karma-inline-spec-reporter/index.js << 'REPORTER_EOF'
var SpecReporter = function(baseReporterDecorator) {{
  baseReporterDecorator(this);
  this.onSpecComplete = function(browser, result) {{
    var name = result.fullName || (result.suite || []).concat(result.description || []).join(' ');
    var status = result.success ? "PASS" : (result.skipped ? "SKIP" : "FAIL");
    this.write(status + ": " + name + "\\n");
  }};
  this.onRunComplete = function(browsers, results) {{
    this.write("TOTAL: " + results.success + " PASS, " + results.failed + " FAIL\\n");
  }};
}};
SpecReporter.$inject = ["baseReporterDecorator"];
module.exports = {{"reporter:inline-spec": ["type", SpecReporter]}};
REPORTER_EOF

  bash /home/generate-karma-config.sh
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "generate-karma-config.sh",
                """#!/bin/bash
cd /home/{pr.repo}
node /home/generate-karma-config.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "generate-karma-config.js",
                """var fs = require('fs');
var path = require('path');
var repoDir = '/home/{pr.repo}';

// Stub scripts/alias.js if missing
var aliasPath = path.join(repoDir, 'scripts/alias.js');
if (!fs.existsSync(aliasPath)) {{
  var resolve = function(p) {{ return path.resolve(repoDir, p); }};
  var aliases = {{
    vue: resolve('src/platforms/web/entry-runtime-with-compiler'),
    compiler: resolve('src/compiler'),
    core: resolve('src/core'),
    shared: resolve('src/shared'),
    web: resolve('src/platforms/web'),
    weex: resolve('src/platforms/weex'),
    server: resolve('src/server'),
    sfc: resolve('src/sfc')
  }};
  fs.mkdirSync(path.dirname(aliasPath), {{ recursive: true }});
  fs.writeFileSync(aliasPath, 'var path = require("path");\\nvar resolve = function(p) {{ return path.resolve(__dirname, "../", p); }};\\nmodule.exports = ' + JSON.stringify(aliases, null, 2).replace(/"/g, "'") + ';\\n');
  console.log('Stubbed scripts/alias.js');
}}

// Stub scripts/feature-flags.js if missing
var featureFlagsPath = path.join(repoDir, 'scripts/feature-flags.js');
if (!fs.existsSync(featureFlagsPath)) {{
  fs.mkdirSync(path.dirname(featureFlagsPath), {{ recursive: true }});
  fs.writeFileSync(featureFlagsPath, 'module.exports = {{}};\\n');
  console.log('Stubbed scripts/feature-flags.js');
}}

var basePaths = [
  path.join(repoDir, 'test/unit/karma.base.config.js'),
  path.join(repoDir, 'build/karma.base.config.js')
];
var baseConfigPath = null;
for (var i = 0; i < basePaths.length; i++) {{
  if (fs.existsSync(basePaths[i])) {{ baseConfigPath = basePaths[i]; break; }}
}}

var reporterPath = path.join(repoDir, 'node_modules/karma-inline-spec-reporter');

if (!baseConfigPath) {{
  // No base config found - generate standalone config for old grunt-based PRs
  console.log('No karma base config found - generating standalone config');
  var testDir = path.join(repoDir, 'test/unit');
  var specDir = path.join(repoDir, 'test/unit/specs');
  var hasSpecs = fs.existsSync(specDir);
  var filesArray = [];
  // Check for common test helper files
  var helperFiles = ['test/unit/lib/util.js', 'test/unit/lib/jquery.js', 'test/unit/lib/indoc_patch.js', 'test/vue.test.js', 'test/unit/utils/chai.js', 'test/unit/utils/prepare.js'];
  helperFiles.forEach(function(f) {{
    if (fs.existsSync(path.join(repoDir, f))) filesArray.push(f);
  }});
  if (hasSpecs) {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/specs/**/*.js');
  }} else {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/**/*.js');
  }}
  // Detect framework: check package.json for jasmine or mocha
  var pkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
  var deps = Object.assign({{}}, pkg.dependencies || {{}}, pkg.devDependencies || {{}});
  var framework = deps['karma-jasmine'] ? 'jasmine' : (deps['karma-mocha'] ? 'mocha' : 'jasmine');
  var preprocessors = {{}};
  if (deps['karma-commonjs']) {{
    preprocessors['src/**/*.js'] = ['commonjs'];
    preprocessors['test/unit/specs/**/*.js'] = ['commonjs'];
  }}

  var configCode = 'module.exports = function(config) {{\\n';
  configCode += '  config.set({{\\n';
  configCode += '    basePath: "' + repoDir.replace(/\\\\/g, '/') + '",\\n';
  configCode += '    frameworks: ["' + framework + '"' + (deps['karma-commonjs'] ? ', "commonjs"' : '') + '],\\n';
  configCode += '    files: ' + JSON.stringify(filesArray) + ',\\n';
  configCode += '    preprocessors: ' + JSON.stringify(preprocessors) + ',\\n';
  configCode += '    plugins: ["karma-*", require("' + reporterPath.replace(/\\\\/g, '/') + '")],\\n';
  configCode += '    reporters: ["inline-spec"],\\n';
  configCode += '    browsers: ["ChromeHeadless"],\\n';
  configCode += '    singleRun: true\\n';
  configCode += '  }});\\n';
  configCode += '}};\\n';

  fs.writeFileSync('/home/karma-custom.conf.js', configCode);
  console.log('Generated standalone /home/karma-custom.conf.js');
  process.exit(0);
}}

var base;
try {{
  delete require.cache[baseConfigPath];
  base = require(baseConfigPath);
}} catch(e) {{
  console.error('Failed to load base config: ' + e.message);
  // Fallback: generate standalone config
  console.log('Falling back to standalone config');
  var pkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
  var deps = Object.assign({{}}, pkg.dependencies || {{}}, pkg.devDependencies || {{}});
  var framework = deps['karma-jasmine'] ? 'jasmine' : (deps['karma-mocha'] ? 'mocha' : 'jasmine');
  var preprocessors = {{}};
  if (deps['karma-commonjs']) {{
    preprocessors['src/**/*.js'] = ['commonjs'];
    preprocessors['test/unit/specs/**/*.js'] = ['commonjs'];
  }}
  var specDir = path.join(repoDir, 'test/unit/specs');
  var hasSpecs = fs.existsSync(specDir);
  var filesArray = [];
  var helperFiles = ['test/unit/lib/util.js', 'test/unit/lib/jquery.js', 'test/unit/lib/indoc_patch.js', 'test/vue.test.js', 'test/unit/utils/chai.js', 'test/unit/utils/prepare.js'];
  helperFiles.forEach(function(f) {{
    if (fs.existsSync(path.join(repoDir, f))) filesArray.push(f);
  }});
  if (hasSpecs) {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/specs/**/*.js');
  }} else {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/**/*.js');
  }}
  var configCode = 'module.exports = function(config) {{\\n';
  configCode += '  config.set({{\\n';
  configCode += '    basePath: "' + repoDir.replace(/\\\\/g, '/') + '",\\n';
  configCode += '    frameworks: ["' + framework + '"' + (deps['karma-commonjs'] ? ', "commonjs"' : '') + '],\\n';
  configCode += '    files: ' + JSON.stringify(filesArray) + ',\\n';
  configCode += '    preprocessors: ' + JSON.stringify(preprocessors) + ',\\n';
  configCode += '    plugins: ["karma-*", require("' + reporterPath.replace(/\\\\/g, '/') + '")],\\n';
  configCode += '    reporters: ["inline-spec"],\\n';
  configCode += '    browsers: ["ChromeHeadless"],\\n';
  configCode += '    singleRun: true\\n';
  configCode += '  }});\\n';
  configCode += '}};\\n';
  fs.writeFileSync('/home/karma-custom.conf.js', configCode);
  console.log('Generated fallback /home/karma-custom.conf.js');
  process.exit(0);
}}

var baseDir = path.dirname(baseConfigPath);

var hasBasePlugins = base.plugins && Array.isArray(base.plugins) && base.plugins.length > 0;

var configCode = 'var path = require("path");\\n';
configCode += 'module.exports = function(config) {{\\n';
configCode += '  var base = require("' + baseConfigPath.replace(/\\\\/g, '/') + '");\\n';
if (hasBasePlugins) {{
  configCode += '  var plugins = base.plugins.concat([\\n';
  configCode += '    "karma-chrome-launcher",\\n';
  configCode += '    require("' + reporterPath.replace(/\\\\/g, '/') + '")\\n';
  configCode += '  ]);\\n';
}} else {{
  configCode += '  var plugins = [\\n';
  configCode += '    "karma-*",\\n';
  configCode += '    require("' + reporterPath.replace(/\\\\/g, '/') + '")\\n';
  configCode += '  ];\\n';
}}
configCode += '  config.set(Object.assign(base, {{\\n';
configCode += '    plugins: plugins,\\n';
configCode += '    reporters: ["inline-spec"],\\n';
configCode += '    browsers: ["ChromeHeadless"],\\n';
configCode += '    singleRun: true,\\n';
configCode += '    basePath: "' + baseDir.replace(/\\\\/g, '/') + '"\\n';
configCode += '  }}));\\n';
configCode += '}};\\n';

fs.writeFileSync('/home/karma-custom.conf.js', configCode);
console.log('Generated /home/karma-custom.conf.js (hasBasePlugins=' + hasBasePlugins + ')');

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
RUNNER=$(cat /home/.test-runner 2>/dev/null || echo "karma")

if [ "$RUNNER" = "vitest" ]; then
  rm -f /tmp/vitest-results.json
  npx vitest run test/unit --reporter=json --outputFile=/tmp/vitest-results.json 2>&1 || true
  node /home/parse-vitest-json.js
else
  ./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true

RUNNER=$(cat /home/.test-runner 2>/dev/null || echo "karma")

if [ "$RUNNER" = "vitest" ]; then
  pnpm install --no-frozen-lockfile 2>/dev/null || true
  rm -f /tmp/vitest-results.json
  npx vitest run test/unit --reporter=json --outputFile=/tmp/vitest-results.json 2>&1 || true
  node /home/parse-vitest-json.js
else
  bash /home/generate-karma-config.sh
  ./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true

RUNNER=$(cat /home/.test-runner 2>/dev/null || echo "karma")

if [ "$RUNNER" = "vitest" ]; then
  pnpm install --no-frozen-lockfile 2>/dev/null || true
  rm -f /tmp/vitest-results.json
  npx vitest run test/unit --reporter=json --outputFile=/tmp/vitest-results.json 2>&1 || true
  node /home/parse-vitest-json.js
else
  bash /home/generate-karma-config.sh
  ./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
fi

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


class ImageDefault12455(Image):
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
        return ImageBase12455(self.pr, self._config)

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

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

yarn install --ignore-engines || yarn install --no-lockfile --ignore-engines || true

# Install karma + friends to a temp prefix, then copy into node_modules
# This avoids npm vs yarn conflicts in the main node_modules
npm install --prefix /tmp/karma-pkg karma@6.4.4 karma-chrome-launcher@latest karma-jasmine karma-commonjs karma-mocha mocha --legacy-peer-deps 2>/dev/null || true
# Copy installed packages from /tmp/karma-pkg/node_modules into project node_modules
if [ -d /tmp/karma-pkg/node_modules ]; then
  cp -rf /tmp/karma-pkg/node_modules/karma node_modules/karma 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-chrome-launcher node_modules/karma-chrome-launcher 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-jasmine node_modules/karma-jasmine 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-commonjs node_modules/karma-commonjs 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-mocha node_modules/karma-mocha 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/mocha node_modules/mocha 2>/dev/null || true
  # Copy all karma transitive deps
  for dir in /tmp/karma-pkg/node_modules/*; do
    pkgname=$(basename "$dir")
    if [ ! -d "node_modules/$pkgname" ]; then
      cp -rf "$dir" "node_modules/$pkgname" 2>/dev/null || true
    fi
  done
fi
mkdir -p node_modules/.bin && ln -sf ../karma/bin/karma node_modules/.bin/karma 2>/dev/null || true
rm -rf node_modules/karma-phantomjs-launcher node_modules/karma-safari-launcher node_modules/karma-firefox-launcher node_modules/karma-ie-launcher 2>/dev/null || true

GRUNTFILE=$(ls gruntfile.js Gruntfile.js 2>/dev/null | head -1)
if [ -n "$GRUNTFILE" ]; then
  npm install grunt-cli --no-save --legacy-peer-deps 2>/dev/null || true
  if [ ! -f test/vue.test.js ] && grep -q "vue.test.js" "$GRUNTFILE" 2>/dev/null; then
    ./node_modules/.bin/grunt instrument 2>/dev/null || ./node_modules/.bin/grunt build 2>/dev/null || true
  fi
fi

mkdir -p node_modules/karma-inline-spec-reporter
cat > node_modules/karma-inline-spec-reporter/index.js << 'REPORTER_EOF'
var SpecReporter = function(baseReporterDecorator) {{
  baseReporterDecorator(this);
  this.onSpecComplete = function(browser, result) {{
    var name = result.fullName || (result.suite || []).concat(result.description || []).join(' ');
    var status = result.success ? "PASS" : (result.skipped ? "SKIP" : "FAIL");
    this.write(status + ": " + name + "\\n");
  }};
  this.onRunComplete = function(browsers, results) {{
    this.write("TOTAL: " + results.success + " PASS, " + results.failed + " FAIL\\n");
  }};
}};
SpecReporter.$inject = ["baseReporterDecorator"];
module.exports = {{"reporter:inline-spec": ["type", SpecReporter]}};
REPORTER_EOF

bash /home/generate-karma-config.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "generate-karma-config.sh",
                """#!/bin/bash
cd /home/{pr.repo}
node /home/generate-karma-config.js

""".format(pr=self.pr),
            ),
            File(
                ".",
                "generate-karma-config.js",
                """var fs = require('fs');
var path = require('path');
var repoDir = '/home/{pr.repo}';

// Stub scripts/alias.js if missing
var aliasPath = path.join(repoDir, 'scripts/alias.js');
if (!fs.existsSync(aliasPath)) {{
  var resolve = function(p) {{ return path.resolve(repoDir, p); }};
  var aliases = {{
    vue: resolve('src/platforms/web/entry-runtime-with-compiler'),
    compiler: resolve('src/compiler'),
    core: resolve('src/core'),
    shared: resolve('src/shared'),
    web: resolve('src/platforms/web'),
    weex: resolve('src/platforms/weex'),
    server: resolve('src/server'),
    sfc: resolve('src/sfc')
  }};
  fs.mkdirSync(path.dirname(aliasPath), {{ recursive: true }});
  fs.writeFileSync(aliasPath, 'var path = require("path");\\nvar resolve = function(p) {{ return path.resolve(__dirname, "../", p); }};\\nmodule.exports = ' + JSON.stringify(aliases, null, 2).replace(/"/g, "'") + ';\\n');
  console.log('Stubbed scripts/alias.js');
}}

// Stub scripts/feature-flags.js if missing
var featureFlagsPath = path.join(repoDir, 'scripts/feature-flags.js');
if (!fs.existsSync(featureFlagsPath)) {{
  fs.mkdirSync(path.dirname(featureFlagsPath), {{ recursive: true }});
  fs.writeFileSync(featureFlagsPath, 'module.exports = {{}};\\n');
  console.log('Stubbed scripts/feature-flags.js');
}}

var basePaths = [
  path.join(repoDir, 'test/unit/karma.base.config.js'),
  path.join(repoDir, 'build/karma.base.config.js')
];
var baseConfigPath = null;
for (var i = 0; i < basePaths.length; i++) {{
  if (fs.existsSync(basePaths[i])) {{ baseConfigPath = basePaths[i]; break; }}
}}

var reporterPath = path.join(repoDir, 'node_modules/karma-inline-spec-reporter');

if (!baseConfigPath) {{
  console.log('No karma base config found - generating standalone config');
  var testDir = path.join(repoDir, 'test/unit');
  var specDir = path.join(repoDir, 'test/unit/specs');
  var hasSpecs = fs.existsSync(specDir);
  var filesArray = [];
  var helperFiles = ['test/unit/lib/util.js', 'test/unit/lib/jquery.js', 'test/unit/lib/indoc_patch.js', 'test/vue.test.js', 'test/unit/utils/chai.js', 'test/unit/utils/prepare.js'];
  helperFiles.forEach(function(f) {{
    if (fs.existsSync(path.join(repoDir, f))) filesArray.push(f);
  }});
  if (hasSpecs) {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/specs/**/*.js');
  }} else {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/**/*.js');
  }}
  var pkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
  var deps = Object.assign({{}}, pkg.dependencies || {{}}, pkg.devDependencies || {{}});
  var framework = deps['karma-jasmine'] ? 'jasmine' : (deps['karma-mocha'] ? 'mocha' : 'jasmine');
  var preprocessors = {{}};
  if (deps['karma-commonjs']) {{
    preprocessors['src/**/*.js'] = ['commonjs'];
    preprocessors['test/unit/lib/indoc_patch.js'] = ['commonjs'];
    preprocessors['test/unit/specs/**/*.js'] = ['commonjs'];
  }}

  var configCode = 'module.exports = function(config) {{\\n';
  configCode += '  config.set({{\\n';
  configCode += '    basePath: "' + repoDir.replace(/\\\\/g, '/') + '",\\n';
  configCode += '    frameworks: ["' + framework + '"' + (deps['karma-commonjs'] ? ', "commonjs"' : '') + '],\\n';
  configCode += '    files: ' + JSON.stringify(filesArray) + ',\\n';
  configCode += '    preprocessors: ' + JSON.stringify(preprocessors) + ',\\n';
  configCode += '    plugins: ["karma-*", require("' + reporterPath.replace(/\\\\/g, '/') + '")],\\n';
  configCode += '    reporters: ["inline-spec"],\\n';
  configCode += '    browsers: ["ChromeHeadless"],\\n';
  configCode += '    singleRun: true\\n';
  configCode += '  }});\\n';
  configCode += '}};\\n';

  fs.writeFileSync('/home/karma-custom.conf.js', configCode);
  console.log('Generated standalone /home/karma-custom.conf.js');
  process.exit(0);
}}

var base;
try {{
  delete require.cache[baseConfigPath];
  base = require(baseConfigPath);
}} catch(e) {{
  console.error('Failed to load base config: ' + e.message);
  console.log('Falling back to standalone config');
  var pkg = JSON.parse(fs.readFileSync(path.join(repoDir, 'package.json'), 'utf8'));
  var deps = Object.assign({{}}, pkg.dependencies || {{}}, pkg.devDependencies || {{}});
  var framework = deps['karma-jasmine'] ? 'jasmine' : (deps['karma-mocha'] ? 'mocha' : 'jasmine');
  var preprocessors = {{}};
  if (deps['karma-commonjs']) {{
    preprocessors['src/**/*.js'] = ['commonjs'];
    preprocessors['test/unit/lib/indoc_patch.js'] = ['commonjs'];
    preprocessors['test/unit/specs/**/*.js'] = ['commonjs'];
  }}
  var specDir = path.join(repoDir, 'test/unit/specs');
  var hasSpecs = fs.existsSync(specDir);
  var filesArray = [];
  var helperFiles = ['test/unit/lib/util.js', 'test/unit/lib/jquery.js', 'test/unit/lib/indoc_patch.js', 'test/vue.test.js', 'test/unit/utils/chai.js', 'test/unit/utils/prepare.js'];
  helperFiles.forEach(function(f) {{
    if (fs.existsSync(path.join(repoDir, f))) filesArray.push(f);
  }});
  if (hasSpecs) {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/specs/**/*.js');
  }} else {{
    filesArray.push('src/**/*.js');
    filesArray.push('test/unit/**/*.js');
  }}
  var configCode = 'module.exports = function(config) {{\\n';
  configCode += '  config.set({{\\n';
  configCode += '    basePath: "' + repoDir.replace(/\\\\/g, '/') + '",\\n';
  configCode += '    frameworks: ["' + framework + '"' + (deps['karma-commonjs'] ? ', "commonjs"' : '') + '],\\n';
  configCode += '    files: ' + JSON.stringify(filesArray) + ',\\n';
  configCode += '    preprocessors: ' + JSON.stringify(preprocessors) + ',\\n';
  configCode += '    plugins: ["karma-*", require("' + reporterPath.replace(/\\\\/g, '/') + '")],\\n';
  configCode += '    reporters: ["inline-spec"],\\n';
  configCode += '    browsers: ["ChromeHeadless"],\\n';
  configCode += '    singleRun: true\\n';
  configCode += '  }});\\n';
  configCode += '}};\\n';
  fs.writeFileSync('/home/karma-custom.conf.js', configCode);
  console.log('Generated fallback /home/karma-custom.conf.js');
  process.exit(0);
}}

var baseDir = path.dirname(baseConfigPath);

var hasBasePlugins = base.plugins && Array.isArray(base.plugins) && base.plugins.length > 0;

var configCode = 'var path = require("path");\\n';
configCode += 'module.exports = function(config) {{\\n';
configCode += '  var base = require("' + baseConfigPath.replace(/\\\\/g, '/') + '");\\n';
if (hasBasePlugins) {{
  configCode += '  var plugins = base.plugins.concat([\\n';
  configCode += '    "karma-chrome-launcher",\\n';
  configCode += '    require("' + reporterPath.replace(/\\\\/g, '/') + '")\\n';
  configCode += '  ]);\\n';
}} else {{
  configCode += '  var plugins = [\\n';
  configCode += '    "karma-*",\\n';
  configCode += '    require("' + reporterPath.replace(/\\\\/g, '/') + '")\\n';
  configCode += '  ];\\n';
}}
configCode += '  config.set(Object.assign(base, {{\\n';
configCode += '    plugins: plugins,\\n';
configCode += '    reporters: ["inline-spec"],\\n';
configCode += '    browsers: ["ChromeHeadless"],\\n';
configCode += '    singleRun: true,\\n';
configCode += '    basePath: "' + baseDir.replace(/\\\\/g, '/') + '"\\n';
configCode += '  }}));\\n';
configCode += '}};\\n';

fs.writeFileSync('/home/karma-custom.conf.js', configCode);
console.log('Generated /home/karma-custom.conf.js (hasBasePlugins=' + hasBasePlugins + ')');

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run

""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
bash /home/generate-karma-config.sh
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true
bash /home/generate-karma-config.sh
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run

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

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


@Instance.register("vuejs", "vue")
class Vue(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 12455 and self.pr.number != 11857:
            return ImageDefault12455(self.pr, self._config)

        return ImageDefault(self.pr, self._config)

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

        re_pass = re.compile(r"^PASS: (.+)$")
        re_fail = re.compile(r"^FAIL: (.+)$")

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            pass_match = re_pass.match(line)
            if pass_match:
                test = pass_match.group(1).strip()
                if test and test != "undefined":
                    passed_tests.add(test)

            fail_match = re_fail.match(line)
            if fail_match:
                test = fail_match.group(1).strip()
                if test and test != "undefined":
                    failed_tests.add(test)

        # If a test appears in both pass and fail (flaky), keep only FAIL
        common = passed_tests & failed_tests
        if common:
            passed_tests -= common

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
