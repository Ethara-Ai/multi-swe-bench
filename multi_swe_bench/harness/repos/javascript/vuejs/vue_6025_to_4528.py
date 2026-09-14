import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "vue_6025_to_4528"
_BASE_TAG = "base-6025_to_4528"
_NODE_IMAGE = "node:18"


_CHECK_GIT_CHANGES = """#!/bin/bash
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


_PREPARE_SH = """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {sha}
bash /home/check_git_changes.sh

yarn install --ignore-engines || yarn install --no-lockfile --ignore-engines || true

npm install --prefix /tmp/karma-pkg karma@6.4.4 karma-chrome-launcher@latest karma-jasmine karma-commonjs karma-mocha mocha --legacy-peer-deps 2>/dev/null || true
if [ -d /tmp/karma-pkg/node_modules ]; then
  cp -rf /tmp/karma-pkg/node_modules/karma node_modules/karma 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-chrome-launcher node_modules/karma-chrome-launcher 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-jasmine node_modules/karma-jasmine 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-commonjs node_modules/karma-commonjs 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/karma-mocha node_modules/karma-mocha 2>/dev/null || true
  cp -rf /tmp/karma-pkg/node_modules/mocha node_modules/mocha 2>/dev/null || true
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
"""


_GEN_KARMA_SH = """#!/bin/bash
cd /home/{repo}
node /home/generate-karma-config.js
"""


_GEN_KARMA_JS = """var fs = require('fs');
var path = require('path');
var repoDir = '/home/{repo}';

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

function standalone() {{
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
  console.log('Generated standalone /home/karma-custom.conf.js');
}}

if (!baseConfigPath) {{
  console.log('No karma base config found - generating standalone config');
  standalone();
  process.exit(0);
}}

var base;
try {{
  delete require.cache[baseConfigPath];
  base = require(baseConfigPath);
}} catch(e) {{
  console.error('Failed to load base config: ' + e.message);
  console.log('Falling back to standalone config');
  standalone();
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
"""


_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
"""


_TEST_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch || git apply --whitespace=nowarn --3way /home/test.patch || true
bash /home/generate-karma-config.sh
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
"""


_FIX_RUN_SH = """#!/bin/bash
set -e

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch || true
bash /home/generate-karma-config.sh
./node_modules/.bin/karma start /home/karma-custom.conf.js --single-run
"""


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

__CERT_SYMLINKS__

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    git \\
    jq \\
    chromium \\
    && rm -rf /var/lib/apt/lists/*

RUN printf '#!/bin/bash\\nexec /usr/bin/chromium --no-sandbox --disable-gpu --headless "$@"\\n' > /usr/local/bin/chromium-no-sandbox && chmod +x /usr/local/bin/chromium-no-sandbox

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__ && \\
    cd /home/__REPO__ && git rev-parse HEAD >/dev/null
"""


_PRUNE = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
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
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
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
"""


def vue_parse_log(test_log: str) -> TestResult:
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    re_pass = re.compile(r"^PASS: (.+)$")
    re_fail = re.compile(r"^FAIL: (.+)$")
    re_skip = re.compile(r"^SKIP: (.+)$")

    for line in test_log.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re_pass.match(line)
        if m:
            t = m.group(1).strip()
            if t and t != "undefined":
                passed_tests.add(t)
        m = re_fail.match(line)
        if m:
            t = m.group(1).strip()
            if t and t != "undefined":
                failed_tests.add(t)
        m = re_skip.match(line)
        if m:
            t = m.group(1).strip()
            if t and t != "undefined":
                skipped_tests.add(t)

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


class VueEraImageBase6025To4528(Image):
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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        body = (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
        )

        sections = [body.strip()]
        for part in (self.clear_env, 'CMD ["/bin/bash"]'):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"

    def _merged_env_block(self) -> str:
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(
            (
                "NODE_OPTIONS=--openssl-legacy-provider",
                "PUPPETEER_SKIP_DOWNLOAD=1",
                "CHROME_BIN=/usr/local/bin/chromium-no-sandbox",
                "CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox",
            )
        )
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )


class VueEraImageDefault6025To4528(Image):
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
        return VueEraImageBase6025To4528(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _render(self, template: str) -> str:
        return template.format(repo=self.pr.repo, sha=self.pr.base.sha)

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", self._render(_PREPARE_SH)),
            File(".", "generate-karma-config.sh", self._render(_GEN_KARMA_SH)),
            File(".", "generate-karma-config.js", self._render(_GEN_KARMA_JS)),
            File(".", "run.sh", self._render(_RUN_SH)),
            File(".", "test-run.sh", self._render(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._render(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        if isinstance(image, str):
            raise ValueError("The dependency of the default image must be an image.")

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        prune = _PRUNE.replace("__BASE_SHA__", self.pr.base.sha)

        sections = [f"FROM {image.image_full_name()}"]
        for part in (
            self.global_env,
            copy_commands,
            "RUN bash /home/prepare.sh",
            f"WORKDIR /home/{self.pr.repo}",
            prune,
            self.clear_env,
        ):
            if part.strip():
                sections.append(part.strip())
        return "\n\n".join(sections) + "\n"


@Instance.register("vuejs", _INTERVAL_NAME)
class VUE_6025_TO_4528(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return VueEraImageDefault6025To4528(self.pr, self._config)

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
        return vue_parse_log(test_log)


Instance.register("vuejs", "4528-6025")(VUE_6025_TO_4528)
