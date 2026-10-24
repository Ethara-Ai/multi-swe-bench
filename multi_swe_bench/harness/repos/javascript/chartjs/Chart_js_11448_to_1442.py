import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NUMBER_INTERVAL = "Chart_js_11448_to_1442"
REPO_DIR = "Chart.js"
BASE_TAG = "base-11448_to_1442"
BASE_IMAGE = "node:18-bullseye"

_DEFAULT_PACKAGES = [
    "ca-certificates",
    "curl",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
    "chromium",
    "fonts-liberation",
]

_CHROMIUM_WRAPPER = (
    "RUN { \\\n"
    "        printf '%s\\n' '#!/bin/bash'; \\\n"
    "        printf '%s' 'exec /usr/bin/chromium --no-sandbox --headless'; \\\n"
    "        printf '%s' ' --disable-gpu --disable-dev-shm-usage'; \\\n"
    "        printf '%s' ' --disable-software-rasterizer'; \\\n"
    "        printf '%s' ' --window-size=1280,1024'; \\\n"
    "        printf '%s' ' --disable-background-timer-throttling'; \\\n"
    "        printf '%s' ' --disable-renderer-backgrounding'; \\\n"
    "        printf '%s' ' --disable-backgrounding-occluded-windows'; \\\n"
    "        printf '%s\\n' ' --remote-debugging-port=9222 \"$@\"'; \\\n"
    "    } > /usr/local/bin/chromium-no-sandbox && \\\n"
    "    chmod +x /usr/local/bin/chromium-no-sandbox"
)

_GIT_INSTEADOF = (
    "RUN git config --global url.\"https://github.com/\".insteadOf \"git://github.com/\" && \\\n"
    "    git config --global url.\"https://github.com/chartjs/chartjs-color\".insteadOf "
    "\"git://github.com/chartjs/color\""
)

_NODE_MULTI = (
    "RUN npm install -g n@9.2.3 && \\\n"
    "    N_PREFIX=/usr/local n install 6 && \\\n"
    "    N_PREFIX=/usr/local n install 10 && \\\n"
    "    n install 18 && \\\n"
    "    npm install -g n@9.2.3"
)

_CLONE_WITH_RETRY = (
    "RUN set -eux; \\\n"
    "    for i in 1 2 3 4 5; do \\\n"
    f"        rm -rf /home/{REPO_DIR}; \\\n"
    f'        if git -C /home clone "${{REPO_URL}}" {REPO_DIR}; then break; fi; \\\n'
    '        echo "clone attempt $i failed, retrying"; sleep 15; \\\n'
    "    done; \\\n"
    f"    test -d /home/{REPO_DIR}/.git"
)

_SELECT_NODE_SH = """select_node() {
    local want=10
    local karma_ver gulp_ver
    if [ -f package.json ]; then
        if grep -q '"rollup"' package.json || ls rollup.config.* >/dev/null 2>&1; then
            want=18
        elif grep -qE '"karma"\\s*:\\s*"[^"]*0\\.12' package.json; then
            want=6
        elif grep -qE '"gulp"\\s*:\\s*"3\\.5' package.json; then
            want=6
        else
            want=10
        fi
    fi
    local cand
    cand=$( (ls -d /usr/local/n/versions/node/${want}* 2>/dev/null || true) | head -1 || true )
    if [ -z "$cand" ]; then cand=$( (ls -d /usr/local/n/versions/node/10* 2>/dev/null || true) | head -1 || true ); fi
    if [ -n "$cand" ]; then export PATH="$cand/bin:$PATH"; fi
    echo "select_node: want=$want using $(node --version)"
}
"""

_KARMA_SETUP_SH = """karma_setup() {
    for kc in karma.conf.ci.js karma.conf.js; do
        if [ -f "$kc" ] && grep -q "'src/\\*\\*/\\*.js': \\['browserify'\\]" "$kc"; then
            sed -i "s#'src/\\*\\*/\\*.js': \\['browserify'\\]#'src/**/*.js': ['browserify'], 'test/**/*.js': ['browserify']#" "$kc" || true
        fi
    done
    if [ -f karma.conf.ci.js ] && grep -q "Firefox" karma.conf.ci.js; then
        sed -i "s/browsers: \\['Firefox'\\]/browsers: ['Chrome_travis_ci']/" karma.conf.ci.js || true
        sed -i "s/reporters: \\['progress', 'html'\\]/reporters: ['spec']/" karma.conf.ci.js || true
    fi
    if [ -f karma.conf.js ]; then
        sed -i "s/browsers: \\['Firefox'\\]/browsers: []/g" karma.conf.js || true
        sed -i "s/config.browsers.push('Chrome');/config.browsers.push('ChromeHeadless');/g" karma.conf.js || true
        sed -i "s/browsers: \\['chrome', 'firefox'\\]/browsers: ['chrome']/" karma.conf.js || true
        sed -i "s/args.browsers || 'chrome,firefox'/args.browsers || 'chrome'/" karma.conf.js || true
        sed -i "s/reporters: \\['progress', 'kjhtml'\\]/reporters: ['spec']/" karma.conf.js || true
        sed -i "s/reporters: \\['progress', 'html'\\]/reporters: ['spec']/" karma.conf.js || true
        sed -i "s/'progress'/'spec'/g" karma.conf.js || true
        sed -i "s/failFast: !!karma.autoWatch/failFast: false/" karma.conf.js || true
        sed -i "s/sourcemap: karma.autoWatch ? 'inline' : false/sourcemap: false/" karma.conf.js || true
    fi
    for kc in karma.conf.ci.js karma.conf.js karma.conf.cjs; do
        if [ -f "$kc" ] && ! grep -q browserNoActivityTimeout "$kc"; then
            sed -i "s/config\\.set({/config.set({ browserNoActivityTimeout: 180000, browserDisconnectTimeout: 20000, browserDisconnectTolerance: 3, captureTimeout: 120000,/" "$kc" || true
        fi
    done
}
"""

_INSTALL_DEPS_SH = """install_deps() {
    local NPM_MAJOR
    NPM_MAJOR=$(npm --version 2>/dev/null | cut -d. -f1)
    NPM_MAJOR=${NPM_MAJOR:-0}
    if [ "$NPM_MAJOR" -ge 7 ] 2>/dev/null; then
        npm install --no-audit --no-fund --legacy-peer-deps 2>&1 \\
            || npm install --no-audit --no-fund --force 2>&1 || true
    else
        npm install --no-audit --no-fund 2>&1 || npm install 2>&1 || true
    fi
    if [ ! -x ./node_modules/.bin/karma ] && grep -qE '"karma"\\s*:\\s*"' package.json 2>/dev/null; then
        KV=$(node -e "try{var p=require('./package.json');process.stdout.write(((p.devDependencies||{}).karma||(p.dependencies||{}).karma||''))}catch(e){}" 2>/dev/null)
        if [ "$NPM_MAJOR" -ge 7 ] 2>/dev/null; then
            npm install --no-save --no-audit --no-fund --legacy-peer-deps "karma@${KV:-latest}" 2>&1 || true
        else
            npm install --no-save --no-audit --no-fund "karma@${KV:-latest}" 2>&1 || true
        fi
    fi
    npm install --no-save --no-audit --no-fund karma-spec-reporter@0.0.32 2>&1 || true
    if grep -qE '"karma"\\s*:\\s*"[^"]*0\\.12' package.json 2>/dev/null; then
        npm install --no-save --no-audit --no-fund color@0.10.1 browserify@13 2>&1 || true
        if [ -f node_modules/color/index.js ] && [ ! -f node_modules/color/dist/color.min.js ]; then
            mkdir -p node_modules/color/dist
            ./node_modules/.bin/browserify node_modules/color/index.js \\
                --standalone Color -o node_modules/color/dist/color.min.js 2>&1 || true
        fi
    fi
}
"""

_RUN_TESTS_SH = """run_tests() {
    export CHROME_BIN=/usr/local/bin/chromium-no-sandbox
    export CHROMIUM_BIN=/usr/local/bin/chromium-no-sandbox
    local st="timeout -k 30 900"
    local KCONF=""
    for k in karma.conf.ci.js karma.conf.js karma.conf.cjs; do
        if [ -f "$k" ]; then KCONF="$k"; break; fi
    done
    if grep -q '"rollup"' package.json 2>/dev/null || ls rollup.config.* >/dev/null 2>&1; then
        $st ./node_modules/.bin/karma start "${KCONF:-karma.conf.js}" --single-run --auto-watch \\
            --browsers chrome --reporters spec --grep 2>&1 || true
    elif [ -x ./node_modules/.bin/gulp ] && grep -qE "'unittest'|\\"unittest\\"|task\\('unittest" gulpfile.js 2>/dev/null; then
        $st ./node_modules/.bin/gulp unittest 2>&1 || true
    elif [ -x ./node_modules/.bin/gulp ] && grep -qE "'test'|\\"test\\"|task\\('test" gulpfile.js 2>/dev/null; then
        $st ./node_modules/.bin/gulp test 2>&1 || true
    elif [ -x ./node_modules/.bin/karma ]; then
        $st ./node_modules/.bin/karma start "${KCONF:-karma.conf.js}" --single-run \\
            --browsers chrome --reporters spec 2>&1 || true
    else
        $st npm test 2>&1 || true
    fi
}
"""

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

cd /home/Chart.js

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: not inside a git repository"
    exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "check_git_changes: work tree dirty"
    git status --porcelain --untracked-files=no
    exit 1
fi

echo "check_git_changes: no uncommitted changes"
"""

_PREPARE_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/Chart.js
git reset --hard
git clean -fdq -e node_modules
git checkout ${BASE_COMMIT}
git clean -fdq -e node_modules
test "$(git rev-parse HEAD)" = "$(git rev-parse "${BASE_COMMIT}")"

""" + _SELECT_NODE_SH + _INSTALL_DEPS_SH + _KARMA_SETUP_SH + """
select_node
install_deps
karma_setup

test -d /home/Chart.js/node_modules
bash /home/check_git_changes.sh || true
"""

_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/Chart.js
""" + _SELECT_NODE_SH + _INSTALL_DEPS_SH + _KARMA_SETUP_SH + _RUN_TESTS_SH + """
select_node
install_deps
karma_setup
run_tests
"""

_TEST_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/Chart.js
git apply --whitespace=nowarn --binary /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --binary --reject /home/test.patch 2>/dev/null || true
""" + _SELECT_NODE_SH + _INSTALL_DEPS_SH + _KARMA_SETUP_SH + _RUN_TESTS_SH + """
select_node
install_deps
karma_setup
run_tests
"""

_FIX_RUN_SH = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/Chart.js
git apply --whitespace=nowarn --binary /home/test.patch 2>/dev/null \
    || git apply --whitespace=nowarn --binary --reject /home/test.patch 2>/dev/null || true
git apply --whitespace=nowarn --binary /home/fix.patch 2>/dev/null \
    || git apply --whitespace=nowarn --binary --reject /home/fix.patch 2>/dev/null || true
""" + _SELECT_NODE_SH + _INSTALL_DEPS_SH + _KARMA_SETUP_SH + _RUN_TESTS_SH + """
select_node
install_deps
karma_setup
run_tests
"""


def _sanitize_patch(patch: str) -> str:
    if not patch:
        return patch
    header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    kept: list[str] = []
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        body = "\n".join(section)
        unusable = "Binary files" in body and "GIT binary patch" not in body
        if not unusable:
            kept.extend(section)

    for line in patch.split("\n"):
        if header.match(line):
            flush()
            section = [line]
        elif section:
            section.append(line)
        else:
            kept.append(line)
    flush()
    return "\n".join(kept)


def _apt_block() -> str:
    packages = " ".join(_DEFAULT_PACKAGES)
    return (
        "RUN set -eux; \\\n"
        "    sed -i '/security.debian.org/d; /bullseye-security/d' /etc/apt/sources.list; \\\n"
        "    ok=0; \\\n"
        "    for host in deb.debian.org ftp.debian.org mirrors.edge.kernel.org ftp.us.debian.org; do \\\n"
        "        sed -i \"s|http://[a-z.]*/debian|http://$host/debian|g\" /etc/apt/sources.list; \\\n"
        "        (apt-get update || apt-get -o Acquire::Check-Valid-Until=false update) || continue; \\\n"
        "        if apt-get install -o Acquire::Retries=20 -o Acquire::http::Timeout=180 \\\n"
        "            -y --no-install-recommends --allow-unauthenticated --fix-missing \\\n"
        f"            {packages}; then ok=1; break; fi; \\\n"
        "        echo \"apt via $host incomplete, trying next mirror\"; \\\n"
        "    done; \\\n"
        "    test \"$ok\" = \"1\"; \\\n"
        "    rm -rf /var/lib/apt/lists/*"
    )


class ChartJsImageBase(Image):
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
        return "node:18-bullseye"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {base_img}",
            DockerfileEnhancer._infrastructure_block(self, base_img).strip(),
            "ENV LC_ALL=C.UTF-8",
        ]

        if self.global_env:
            sections.append(self.global_env)

        sections.append("WORKDIR /home/")
        sections.append(_apt_block())
        sections.append(_NODE_MULTI)
        sections.append(_CHROMIUM_WRAPPER)
        sections.append(_GIT_INSTEADOF)
        sections.append("RUN git config --global --add safe.directory '*'")
        sections.append(_CLONE_WITH_RETRY)
        sections.append(f"WORKDIR /home/{REPO_DIR}")
        sections.append(
            "RUN git remote remove origin 2>/dev/null || true; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            "    git config --local gc.auto 0"
        )

        if self.clear_env:
            sections.append(self.clear_env)

        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


class ChartJsImageDefault(Image):
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
        return ChartJsImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", _sanitize_patch(self.pr.fix_patch)),
            File(".", "test.patch", _sanitize_patch(self.pr.test_patch)),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", _PREPARE_SH),
            File(".", "run.sh", _RUN_SH),
            File(".", "test-run.sh", _TEST_RUN_SH),
            File(".", "fix-run.sh", _FIX_RUN_SH),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        base_name = base.image_full_name() if isinstance(base, Image) else base
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())
        sha = self.pr.base.sha
        scrub = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", sha).strip()

        header = (
            f"FROM {base_name}\n\n"
            f'ARG BASE_COMMIT="{sha}"\n'
            "ENV BASE_COMMIT=${BASE_COMMIT}\n\n"
            f"{self.global_env}\n\n"
            f"WORKDIR /home/{REPO_DIR}\n\n"
            f"{copy_commands}\n"
            f"RUN git reset --hard && git checkout {sha}\n\n"
            f"RUN bash /home/prepare.sh\n\n"
        )
        tail = f"\n\n{self.clear_env}\n\nCMD [\"/bin/bash\"]\n"
        return header + scrub + tail


@Instance.register("chartjs", NUMBER_INTERVAL)
class CHART_JS_11448_TO_1442(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ChartJsImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_spec = re.compile(r"^(\s*)(✓|√|✗|×|-)\s+(.*\S)\s*$")
        re_suite = re.compile(r"^(\s*)(\S.*\S|\S)\s*$")
        re_noise = re.compile(
            r"^\s*(HeadlessChrome|Chrome|Chromium|Firefox|PhantomJS)\b"
            r"|^\s*(Executed|TOTAL|SUCCESS|FAILED|Finished|START|LOG|WARN|INFO|ERROR)\b"
            r"|^\s*\d+\)"
            r"|^\s*at\s"
            r"|^\s*\d+ (spec|test)s?, "
        )

        stack: list[tuple[int, str]] = []
        last_spec_indent: Optional[int] = None
        for raw in log.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue

            m = re_spec.match(line)
            if m:
                indent, mark, title = len(m.group(1)), m.group(2), m.group(3)
                last_spec_indent = indent
                prefix = "::".join(name for lvl, name in stack if lvl < indent)
                test_id = f"{prefix}::{title}" if prefix else title
                if mark in ("✓", "√"):
                    if test_id not in failed_tests:
                        skipped_tests.discard(test_id)
                        passed_tests.add(test_id)
                elif mark in ("✗", "×"):
                    passed_tests.discard(test_id)
                    skipped_tests.discard(test_id)
                    failed_tests.add(test_id)
                else:
                    if test_id not in passed_tests and test_id not in failed_tests:
                        skipped_tests.add(test_id)
                continue

            if re_noise.match(line):
                continue

            m = re_suite.match(line)
            if m:
                ws, name = m.group(1), m.group(2)
                indent = len(ws)
                if indent == 0 or "\t" in ws:
                    continue
                if last_spec_indent is not None and indent > last_spec_indent:
                    continue
                last_spec_indent = None
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                stack.append((indent, name))

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
