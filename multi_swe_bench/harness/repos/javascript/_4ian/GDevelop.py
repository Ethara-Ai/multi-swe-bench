import re
import textwrap
import xml.etree.ElementTree as ET
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class GDevelopImageBase(Image):
    """Base image for GDevelop: node:18 + cmake/g++ for C++ tests + chromium for karma."""

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
WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential \\
    cmake \\
    g++ \\
    pkg-config \\
    chromium \\
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true

{code}
"""


class GDevelopImageDefault(Image):
    """Per-PR image that copies patches and shell scripts."""

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
        return GDevelopImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _test_runner_body(self) -> str:
        """Shared test-execution logic used by run.sh, test-run.sh, fix-run.sh.

        Runs three test systems:
          1. C++ Core tests (cmake + catch.hpp with -r xml)
          2. GDJS karma tests (esbuild + karma/mocha with ChromeHeadless --no-sandbox)
          3. GDevelop.js jest tests (using pre-built libGD.js from S3)
        """
        sha = self.pr.base.sha
        s3_base = "https://s3.amazonaws.com/gdevelop-gdevelop.js/master/commit"

        return r"""
echo "==== GDEVELOP TEST RUNNER ===="

# ---------- C++ Core tests ----------
HAS_CPP_TESTS=0
if ls Core/tests/*.cpp 1>/dev/null 2>&1 && [ -f "CMakeLists.txt" ]; then
    HAS_CPP_TESTS=1
fi
if [ "$HAS_CPP_TESTS" = "1" ]; then
    echo "==== CPP_TESTS_START ===="
    mkdir -p build
    cd build
    cmake -DBUILD_TESTS=TRUE -DBUILD_CORE=TRUE -DBUILD_GDJS=FALSE -DBUILD_EXTENSIONS=FALSE .. 2>&1 || true
    make -j$(nproc) GDCore_tests 2>&1 || true
    cd ..
    GDCORE_BIN=""
    if [ -f "build/Core/GDCore_tests" ]; then
        GDCORE_BIN="build/Core/GDCore_tests"
    elif [ -f "Binaries/Output/Release_Linux/GDCore_tests" ]; then
        GDCORE_BIN="Binaries/Output/Release_Linux/GDCore_tests"
    fi
    if [ -n "$GDCORE_BIN" ]; then
        LD_LIBRARY_PATH=./Binaries/Output/Release_Linux \
            ./$GDCORE_BIN -r xml 2>&1 || true
    else
        echo "GDCore_tests binary not found, skipping C++ tests"
    fi
    echo "==== CPP_TESTS_END ===="
fi

# ---------- GDJS karma tests ----------
HAS_KARMA_TESTS=0
if [ -d "GDJS/tests" ] && [ -f "GDJS/tests/package.json" ]; then
    HAS_KARMA_TESTS=1
fi
if [ "$HAS_KARMA_TESTS" = "1" ]; then
    echo "==== KARMA_TESTS_START ===="
    # Build GDJS runtime (esbuild TS->JS) into newIDE/app/resources/GDJS/Runtime/
    cd GDJS
    npm install 2>&1 || true
    if [ -f "scripts/build.js" ]; then
        node scripts/build.js 2>&1 || true
    fi
    cd tests
    npm install 2>&1 || true
    cd ../..

    # Create custom karma launcher with inline result reporter
    # The inline reporter outputs machine-readable lines for each test:
    #   KARMA_TEST_RESULT::PASS::<full test name>
    #   KARMA_TEST_RESULT::FAIL::<full test name>
    #   KARMA_TEST_RESULT::SKIP::<full test name>
    cat > /tmp/run_karma.js << 'KARMA_SCRIPT'
var karma = require('/home/GDevelop/GDJS/tests/node_modules/karma');
var path = require('path');
var cfg = karma.config;

var karmaConfigPath = path.resolve('/home/GDevelop/GDJS/tests/karma.conf.js');

// Custom inline reporter that prints individual test results
// Karma 1.x reporter plugin format: factory function with $inject
var ResultReporterFactory = function(baseReporterDecorator) {
    baseReporterDecorator(this);
    this.onSpecComplete = function(browser, result) {
        var fullName = result.suite.join(' ') + ' ' + result.description;
        if (result.skipped) {
            console.log('KARMA_TEST_RESULT::SKIP::' + fullName);
        } else if (result.success) {
            console.log('KARMA_TEST_RESULT::PASS::' + fullName);
        } else {
            console.log('KARMA_TEST_RESULT::FAIL::' + fullName);
        }
    };
    this.onRunComplete = function(browsers, results) {
        console.log('KARMA_SUMMARY::executed=' + results.success + results.failed +
            '::passed=' + results.success +
            '::failed=' + results.failed +
            '::skipped=' + (results.disconnected ? 0 : 0));
    };
};
ResultReporterFactory.$inject = ['baseReporterDecorator'];

var overrides = {
    browsers: ['ChromeHeadlessCustom'],
    customLaunchers: {
        ChromeHeadlessCustom: {
            base: 'ChromeHeadless',
            flags: ['--no-sandbox', '--disable-gpu']
        }
    },
    singleRun: true,
    plugins: [
        'karma-*',
        { 'reporter:gdresult': ['type', ResultReporterFactory] }
    ],
    reporters: ['gdresult']
};

function startServer(config) {
    var server = new karma.Server(config, function(exitCode) {
        process.exit(exitCode);
    });
    server.start();
}

// Detect karma version: v6+ parseConfig returns a Promise, v1.x returns synchronously
var result;
try {
    result = cfg.parseConfig(karmaConfigPath, overrides);
} catch(e) {
    console.error('Karma config parse failed:', e.message);
    process.exit(1);
}

if (result && typeof result.then === 'function') {
    result.then(startServer).catch(function(err) {
        console.error('Karma config parse failed:', err.message);
        process.exit(1);
    });
} else {
    startServer(result);
}
KARMA_SCRIPT
    node /tmp/run_karma.js 2>&1 || true
    echo "==== KARMA_TESTS_END ===="
fi

""" + f"""
# ---------- GDevelop.js jest tests (pre-built libGD.js from S3) ----------
HAS_JEST_TESTS=0
if [ -d "GDevelop.js/__tests__" ] && [ -f "GDevelop.js/package.json" ]; then
    HAS_JEST_TESTS=1
fi
if [ "$HAS_JEST_TESTS" = "1" ]; then
    echo "==== JEST_TESTS_START ===="
    mkdir -p Binaries/embuild/GDevelop.js
    echo "Downloading pre-built libGD.js from S3..."
    LIBGD_OK=1
    curl -sS -f -o Binaries/embuild/GDevelop.js/libGD.js \\
        "{s3_base}/{sha}/libGD.js" 2>&1 || LIBGD_OK=0
    curl -sS -f -o Binaries/embuild/GDevelop.js/libGD.wasm \\
        "{s3_base}/{sha}/libGD.wasm" 2>&1 || LIBGD_OK=0
    if [ "$LIBGD_OK" = "1" ]; then
        cd GDevelop.js
        npm install 2>&1 || true
        npx jest --verbose --no-coverage --forceExit 2>&1 || true
        cd ..
    else
        echo "libGD.js download failed, skipping jest tests"
    fi
    echo "==== JEST_TESTS_END ===="
fi

echo "==== GDEVELOP TEST RUNNER DONE ===="
"""

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        runner = self._test_runner_body()
        header = f"#!/bin/bash\nset -e\n\ncd /home/{repo}\n"

        # Script to strip binary diff hunks from patch files.
        # Binary hunks use "Binary files ... differ" or "GIT binary patch" and
        # cause "git apply" to fail when the full binary content isn't included.
        strip_binary = (
            "#!/usr/bin/env python3\n"
            '"""Strip binary diff hunks from a unified patch file."""\n'
            "import sys\n"
            "\n"
            "def strip_binary_hunks(patch_text):\n"
            "    chunks = []\n"
            "    current_chunk_lines = []\n"
            "    is_binary = False\n"
            "    for line in patch_text.split('\\n'):\n"
            "        if line.startswith('diff --git '):\n"
            "            if current_chunk_lines and not is_binary:\n"
            "                chunks.append('\\n'.join(current_chunk_lines))\n"
            "            current_chunk_lines = [line]\n"
            "            is_binary = False\n"
            "        else:\n"
            "            current_chunk_lines.append(line)\n"
            "            if 'Binary files' in line or 'GIT binary patch' in line:\n"
            "                is_binary = True\n"
            "    if current_chunk_lines and not is_binary:\n"
            "        chunks.append('\\n'.join(current_chunk_lines))\n"
            "    return '\\n'.join(chunks)\n"
            "\n"
            "for path in sys.argv[1:]:\n"
            "    with open(path, 'r', errors='replace') as f:\n"
            "        content = f.read()\n"
            "    filtered = strip_binary_hunks(content)\n"
            "    with open(path, 'w') as f:\n"
            "        f.write(filtered)\n"
        )

        # Apply patches with binary stripping: first strip binary hunks,
        # then apply. This ensures text-only diffs are applied cleanly.
        apply_test = (
            "python3 /home/strip_binary.py /home/test.patch\n"
            "git apply --whitespace=nowarn /home/test.patch || true\n"
        )
        apply_both = (
            "python3 /home/strip_binary.py /home/test.patch /home/fix.patch\n"
            "git apply --whitespace=nowarn /home/test.patch || true\n"
            "git apply --whitespace=nowarn /home/fix.patch || true\n"
        )

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "strip_binary.py", strip_binary),
            File(
                ".",
                "check_git_changes.sh",
                "#!/bin/bash\n"
                "set -e\n"
                "\n"
                "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
                '  echo "check_git_changes: Not inside a git repository"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                "if [[ -n $(git status --porcelain) ]]; then\n"
                '  echo "check_git_changes: Uncommitted changes"\n'
                "  exit 1\n"
                "fi\n"
                "\n"
                'echo "check_git_changes: No uncommitted changes"\n'
                "exit 0\n",
            ),
            File(
                ".",
                "prepare.sh",
                f"#!/bin/bash\n"
                f"set -e\n"
                f"\n"
                f"cd /home/{repo}\n"
                f"git reset --hard\n"
                f"bash /home/check_git_changes.sh\n"
                f"git checkout {sha}\n"
                f"bash /home/check_git_changes.sh\n",
            ),
            File(".", "run.sh", header + runner),
            File(
                ".",
                "test-run.sh",
                header + apply_test + runner,
            ),
            File(
                ".",
                "fix-run.sh",
                header + apply_both + runner,
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


@Instance.register("4ian", "GDevelop")
class GDevelop(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GDevelopImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        self._parse_cpp_xml(test_log, passed_tests, failed_tests, skipped_tests)
        self._parse_karma(test_log, passed_tests, failed_tests, skipped_tests)
        self._parse_jest(test_log, passed_tests, failed_tests, skipped_tests)

        # Filter out flaky Firebase tests (external service dependency,
        # randomly PASS/FAIL causing false regressions).
        firebase_filter = lambda s: {t for t in s if "Firebase" not in t}
        passed_tests = firebase_filter(passed_tests)
        failed_tests = firebase_filter(failed_tests)
        skipped_tests = firebase_filter(skipped_tests)

        # Disjointness: failed > passed > skipped
        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    def _parse_cpp_xml(
        self,
        log: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        """Parse catch.hpp -r xml output between CPP_TESTS_START/END markers.

        XML format:
          <Catch>
            <Group>
              <TestCase name="TestName">
                <OverallResult success="true"/>
              </TestCase>
              ...
            </Group>
            <OverallResults .../>
          </Catch>
        """
        start_marker = "==== CPP_TESTS_START ===="
        end_marker = "==== CPP_TESTS_END ===="

        start_idx = log.find(start_marker)
        end_idx = log.find(end_marker)
        if start_idx == -1 or end_idx == -1:
            return

        section = log[start_idx + len(start_marker):end_idx]

        # Extract <Catch>...</Catch> XML block
        xml_start = section.find("<Catch")
        xml_end = section.find("</Catch>")
        if xml_start == -1 or xml_end == -1:
            # Fallback: try to parse compact/default output
            self._parse_cpp_fallback(section, passed, failed, skipped)
            return

        xml_str = section[xml_start:xml_end + len("</Catch>")]

        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            # XML might be malformed if tests crashed mid-output
            self._parse_cpp_fallback(section, passed, failed, skipped)
            return

        for test_case in root.iter("TestCase"):
            name = test_case.get("name", "").strip()
            if not name:
                continue
            test_name = f"cpp::{name}"

            result_elem = test_case.find("OverallResult")
            if result_elem is not None:
                success = result_elem.get("success", "false").lower() == "true"
                if success:
                    passed.add(test_name)
                else:
                    failed.add(test_name)
            else:
                # No result element means test may have crashed
                failed.add(test_name)

    def _parse_cpp_fallback(
        self,
        section: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        """Fallback parser for catch.hpp default/compact output.

        Handles:
          "All tests passed (N assertions in M test cases)"
          "test cases: M | P passed | F failed"
        Since individual test names aren't available in default output,
        we create a single synthetic test name.
        """
        all_passed_re = re.compile(
            r"All tests passed \((\d+) assertions? in (\d+) test cases?\)"
        )
        summary_re = re.compile(
            r"test cases:\s*(\d+)\s*\|\s*(\d+)\s*passed"
            r"(?:\s*\|\s*(\d+)\s*failed)?",
            re.IGNORECASE,
        )

        for line in section.splitlines():
            stripped = line.strip()

            m = all_passed_re.match(stripped)
            if m:
                passed.add("cpp::all_tests")
                return

            m = summary_re.match(stripped)
            if m:
                total = int(m.group(1))
                n_passed = int(m.group(2))
                n_failed = int(m.group(3)) if m.group(3) else 0
                if n_failed == 0 and n_passed > 0:
                    passed.add("cpp::all_tests")
                elif n_failed > 0:
                    failed.add("cpp::all_tests")
                return

    def _parse_karma(
        self,
        log: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        start_marker = "==== KARMA_TESTS_START ===="
        end_marker = "==== KARMA_TESTS_END ===="

        start_idx = log.find(start_marker)
        end_idx = log.find(end_marker)
        if start_idx == -1 or end_idx == -1:
            return

        section = log[start_idx + len(start_marker):end_idx]

        result_re = re.compile(r"^KARMA_TEST_RESULT::(PASS|FAIL|SKIP)::(.+)$")

        found_results = False
        for raw_line in section.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_line).strip()
            if not line:
                continue
            m = result_re.match(line)
            if m:
                found_results = True
                status = m.group(1)
                name = f"karma::{m.group(2)}"
                if status == "PASS":
                    passed.add(name)
                elif status == "FAIL":
                    failed.add(name)
                elif status == "SKIP":
                    skipped.add(name)

        if not found_results:
            self._parse_karma_fallback(section.splitlines(), passed, failed, skipped)

    def _parse_karma_fallback(
        self,
        lines: list[str],
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        fail_re = re.compile(
            r"(?:HeadlessChrome|Chrome|Chromium)\s+[\d.]+\s+\([^)]+\)\s+"
            r"(.+?)\s+FAILED\s*$"
        )
        summary_re = re.compile(
            r"(?:HeadlessChrome|Chrome|Chromium)\s+[\d.]+\s+\([^)]+\):\s+"
            r"Executed\s+(\d+)\s+of\s+(\d+)\s+"
            r"(?:\((\d+)\s+FAILED\))?\s*"
            r"(?:SUCCESS)?\s*"
            r"(?:\(skipped\s+(\d+)\))?"
        )

        failed_names: list[str] = []
        n_failed_summary = 0
        n_skipped_summary = 0
        executed = 0

        for raw_line in lines:
            line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw_line).strip()
            if not line:
                continue

            m = fail_re.match(line)
            if m:
                name = m.group(1).strip()
                if name:
                    failed_names.append(name)
                continue

            m = summary_re.match(line)
            if m:
                executed = int(m.group(1))
                n_failed_summary = int(m.group(3)) if m.group(3) else 0
                n_skipped_summary = int(m.group(4)) if m.group(4) else 0

        for name in failed_names:
            failed.add(f"karma::{name}")

        n_passed = executed - n_failed_summary
        if n_passed > 0:
            passed.add("karma::all_passing_tests")

        if n_skipped_summary > 0:
            skipped.add("karma::skipped_tests")

    def _parse_jest(
        self,
        log: str,
        passed: set[str],
        failed: set[str],
        skipped: set[str],
    ) -> None:
        start_marker = "==== JEST_TESTS_START ===="
        end_marker = "==== JEST_TESTS_END ===="

        start_idx = log.find(start_marker)
        end_idx = log.find(end_marker)
        if start_idx == -1 or end_idx == -1:
            return

        section = log[start_idx + len(start_marker):end_idx]

        # Jest verbose output: ✓ / ✕ / ○ for pass/fail/skip
        # Jest default output: PASS/FAIL suite lines + ✓/✕/○ individual lines
        # We track the current suite context for namespacing.
        pass_re = re.compile(r"^\s*[✓✔]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        fail_re = re.compile(r"^\s*[✕✗✘×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$")
        skip_re = re.compile(r"^\s*[○◌]\s+(?:skipped\s+)?(.+?)$")
        suite_re = re.compile(r"^\s*(PASS|FAIL)\s+(.+)$")
        describe_re = re.compile(r"^\s{2,}(\S.+)$")

        current_suite = ""
        current_describe = ""
        strip_ansi = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        for raw_line in section.splitlines():
            line = strip_ansi.sub("", raw_line)
            if not line.strip():
                current_describe = ""
                continue

            m = suite_re.match(line)
            if m:
                current_suite = m.group(2).strip()
                current_describe = ""
                continue

            m = describe_re.match(line)
            if m:
                text = m.group(1).strip()
                if not any(c in text for c in ["✓", "✕", "✗", "✘", "×", "✔", "○", "◌"]):
                    current_describe = text
                    continue

            m = pass_re.match(line)
            if m:
                name = m.group(1).strip()
                full = f"jest::{current_describe} {name}".strip() if current_describe else f"jest::{name}"
                passed.add(full)
                continue

            m = fail_re.match(line)
            if m:
                name = m.group(1).strip()
                full = f"jest::{current_describe} {name}".strip() if current_describe else f"jest::{name}"
                failed.add(full)
                continue

            m = skip_re.match(line)
            if m:
                name = m.group(1).strip()
                full = f"jest::{current_describe} {name}".strip() if current_describe else f"jest::{name}"
                skipped.add(full)
