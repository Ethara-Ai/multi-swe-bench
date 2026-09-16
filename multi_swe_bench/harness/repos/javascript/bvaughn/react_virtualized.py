import re
from typing import Optional

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:20"
BASE_TAG = "base"
RESULTS_MARKER = "----- per-test results -----"

TEST_UTILS_JS = """import ReactDOM from 'react-dom'

export function render (markup) {
  if (!render._mountNode) {
    render._mountNode = document.createElement('div')
    document.body.appendChild(render._mountNode)
  }
  return ReactDOM.render(markup, render._mountNode)
}

render.unmount = function () {
  if (render._mountNode) {
    ReactDOM.unmountComponentAtNode(render._mountNode)
    document.body.removeChild(render._mountNode)
    render._mountNode = null
  }
}

afterEach(render.unmount)
"""

_PASS = re.compile(r"^PASS: (.+)$", re.M)
_FAIL = re.compile(r"^FAIL: (.+)$", re.M)
_SKIP = re.compile(r"^SKIP: (.+)$", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

REPORTER_JS = """var SpecReporter = function (baseReporterDecorator) {
  baseReporterDecorator(this);
  this.onSpecComplete = function (browser, result) {
    var name =
      result.fullName ||
      (result.suite || []).concat(result.description || []).join(" ");
    var status = result.success ? "PASS" : (result.skipped ? "SKIP" : "FAIL");
    this.write(status + ": " + name + "\\n");
  };
  this.onRunComplete = function (browsers, results) {
    this.write("TOTAL: " + results.success + " PASS, " + results.failed + " FAIL\\n");
  };
};
SpecReporter.$inject = ["baseReporterDecorator"];
module.exports = {"reporter:inline-spec": ["type", SpecReporter]};
"""

KARMA_CONF_JS = """var path = require("path");

var webpackTestConfig = {
  devtool: "inline-source-map",
  module: {
    loaders: [
      {
        test: /\\.js$/,
        loaders: ["babel"],
        include: path.join(__dirname, "source")
      },
      {
        test: /\\.less$/,
        loaders: ["style", "css", "less"],
        include: path.join(__dirname, "source")
      },
      {
        test: /\\.css$/,
        loaders: ["style", "css?modules&importLoaders=1"],
        include: path.join(__dirname, "source")
      }
    ]
  }
};

module.exports = function (config) {
  config.set({
    browsers: ["ChromeHeadlessNoSandbox"],
    customLaunchers: {
      ChromeHeadlessNoSandbox: {
        base: "ChromeHeadless",
        flags: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"]
      }
    },
    frameworks: ["jasmine"],
    files: ["source/msb-entry.js"],
    preprocessors: {
      "source/msb-entry.js": ["webpack", "sourcemap"]
    },
    singleRun: true,
    plugins: [
      "karma-jasmine",
      "karma-webpack",
      "karma-sourcemap-loader",
      "karma-chrome-launcher",
      "karma-inline-spec-reporter"
    ],
    reporters: ["inline-spec"],
    webpack: webpackTestConfig,
    webpackMiddleware: {
      stats: "errors-only"
    }
  });
};
"""


def _run_tests_sh(repo: str) -> str:
    majority_awk = (
        "{ i=index($0,\": \"); s=substr($0,1,i-1); m=substr($0,i+2); "
        "cnt[m,s]++; seen[m]=1 } "
        "END { split(\"PASS FAIL SKIP\",st,\" \"); "
        "for (m in seen) { b=\"\"; bn=0; "
        "for (j=1;j<=3;j++) { c=cnt[m,st[j]]+0; if (c>bn) { bn=c; b=st[j] } } "
        "print b\": \"m } }"
    )
    return (
        "#!/bin/bash\n"
        f"cd /home/{repo}\n"
        "cp /home/TestUtils.js source/TestUtils.js\n"
        "RESULTS=/tmp/msb-results.txt\n"
        ': > "$RESULTS"\n'
        "BASE_ENTRY=/tmp/msb-base-entry.js\n"
        "grep -v 'require.context' source/tests.js"
        " | grep -v 'tests.keys()' > \"$BASE_ENTRY\"\n"
        "SUITES=$(find source -type f \\( -name '*.test.js' -o -name '*.test.jsx' \\)"
        " | sort)\n"
        "for suite in $SUITES; do\n"
        '  rel="./${suite#source/}"\n'
        '  cp "$BASE_ENTRY" source/msb-entry.js\n'
        "  echo \"require('$rel')\" >> source/msb-entry.js\n"
        '  log="/tmp/msb-$(echo "$suite" | tr / _).log"\n'
        "  NODE_ENV=test ./node_modules/.bin/karma start karma.conf.final.js"
        ' --single-run > "$log" 2>&1\n'
        "  grep -E '^(PASS|FAIL|SKIP): ' \"$log\" | sort > \"$log.r1\"\n"
        "  NODE_ENV=test ./node_modules/.bin/karma start karma.conf.final.js"
        ' --single-run > "$log" 2>&1\n'
        "  grep -E '^(PASS|FAIL|SKIP): ' \"$log\" | sort > \"$log.r2\"\n"
        '  if cmp -s "$log.r1" "$log.r2"; then\n'
        '    cp "$log.r1" "$log.final"\n'
        "  else\n"
        '    echo "----- karma: $suite ----- disagree, tie-breaker run"\n'
        "    NODE_ENV=test ./node_modules/.bin/karma start karma.conf.final.js"
        ' --single-run > "$log" 2>&1\n'
        "    grep -E '^(PASS|FAIL|SKIP): ' \"$log\" | sort > \"$log.r3\"\n"
        "    cat \"$log.r1\" \"$log.r2\" \"$log.r3\""
        f" | awk '{majority_awk}' | sort > \"$log.final\"\n"
        "  fi\n"
        '  echo "----- karma: $suite -----"\n'
        '  tail -15 "$log"\n'
        '  while IFS= read -r line; do\n'
        '    status="${line%%:*}"\n'
        '    name="${line#*: }"\n'
        '    echo "$status: $suite > $name" >> "$RESULTS"\n'
        '  done < "$log.final"\n'
        "done\n"
        "rm -f source/msb-entry.js\n"
        f'echo "{RESULTS_MARKER}"\n'
        'cat "$RESULTS"\n'
    )


class ReactVirtualizedImageBase(Image):
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
        base_img = self.dependency()
        infra = DockerfileEnhancer._infrastructure_block(self, base_img).rstrip("\n")
        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {base_img}

{infra}

ENV NODE_OPTIONS=--openssl-legacy-provider
ENV CHROME_BIN=/usr/bin/chromium
ENV CI=true
ENV NO_COLOR=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
        chromium git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class ReactVirtualizedImageDefault(Image):
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
        return ReactVirtualizedImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "git rev-parse --is-inside-work-tree > /dev/null\n"
            "git status --porcelain\n"
            'test -z "$(git status --porcelain)"\n'
            'echo "check_git_changes: No uncommitted changes"\n'
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout --detach "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "for pattern in karma.conf.final.js source/msb-entry.js node_modules/"
            " package-lock.json; do\n"
            '  grep -qxF "$pattern" .git/info/exclude'
            ' || echo "$pattern" >> .git/info/exclude\n'
            "done\n"
            "npm install --ignore-scripts --no-audit --no-fund\n"
            "npm install karma-chrome-launcher --no-save --ignore-scripts"
            " --no-audit --no-fund\n"
            "mkdir -p node_modules/karma-inline-spec-reporter\n"
            "cp /home/reporter.js node_modules/karma-inline-spec-reporter/index.js\n"
            f"cp /home/karma.conf.final.js /home/{repo}/karma.conf.final.js\n"
            "test -f source/tests.js\n"
            "./node_modules/.bin/karma --version\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "bash /home/run_tests.sh\n"
        )

        test_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch"
            " || git apply --whitespace=nowarn --3way /home/test.patch\n"
            "bash /home/run_tests.sh\n"
        )

        fix_run_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f"cd /home/{repo}\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch"
            " || git apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch\n"
            "bash /home/run_tests.sh\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "reporter.js", REPORTER_JS),
            File(".", "karma.conf.final.js", KARMA_CONF_JS),
            File(".", "TestUtils.js", TEST_UTILS_JS),
            File(".", "run_tests.sh", _run_tests_sh(repo)),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        copies = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        return f"""FROM {image.image_full_name()}

{copies}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

WORKDIR /home/{self.pr.repo}

{Image._HARDENING_BLOCK}
"""


@Instance.register("bvaughn", "react-virtualized")
class ReactVirtualized(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ReactVirtualizedImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        section = _ANSI.sub("", test_log).rsplit(RESULTS_MARKER, 1)[-1]
        failed_tests = {t.strip() for t in _FAIL.findall(section)}
        passed_tests = {t.strip() for t in _PASS.findall(section)} - failed_tests
        skipped_tests = (
            {t.strip() for t in _SKIP.findall(section)} - failed_tests - passed_tests
        )
        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
