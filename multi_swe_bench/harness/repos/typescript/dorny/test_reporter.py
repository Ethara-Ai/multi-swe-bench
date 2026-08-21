import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


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

    def dependency(self) -> str:
        return "node:20"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The repo's own `npm test` already emits JUnit XML via the jest-junit
        # reporter. Running that and printing the XML gives parse_log exact
        # classname/name pairs, instead of jest's console output where the test name
        # is indented under a describe block and only the FILE appears at the margin.
        # package.json pins the reporter's output location:
        #   "jest-junit": { "outputDirectory": "__tests__/__results__",
        #                   "outputName": "jest-junit.xml" }
        # so it is NOT junit.xml in the repo root. The find is a safety net in case a
        # later commit moves it again; without one, a relocated file silently yields
        # an empty log, which reads downstream as "these tests do not exist".
        cmd = (
            "npx jest --ci --reporters=default --reporters=jest-junit || true\n"
            "echo '--- JUNIT XML ---'\n"
            "cat __tests__/__results__/jest-junit.xml 2>/dev/null "
            "|| find . -name 'jest-junit.xml' -not -path './node_modules/*' "
            "-exec cat {} + 2>/dev/null "
            "|| echo 'no junit xml produced'"
        )
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{cmd}
""".format(pr=self.pr, cmd=cmd),
            ),
        ]

    def dockerfile(self) -> str:
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {self.dependency()}
# npm's progress bar and colour output are drawn with non-ASCII characters. The
# harness decodes buildx output with the platform default codec (cp1252 on Windows),
# where those bytes are undefined and abort the build with "'charmap' codec can't
# decode byte ...".
ENV NPM_CONFIG_PROGRESS=false
ENV NPM_CONFIG_COLOR=false
ENV NO_COLOR=1
ENV FORCE_COLOR=0
# Many JS suites change behaviour without this - skipping tests, adding watch mode,
# or prompting. jest also uses it to disable interactive output.
ENV CI=true

RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

# DockerfileEnhancer rewrites the clone above and appends its own WORKDIR, reset
# --hard and checkout BASE_COMMIT, then the history-scrub block whose assertions fail
# the build unless HEAD is exactly BASE_COMMIT. Repeating any of that here would be
# dead code. The WORKDIR is kept so the npm steps below do not depend silently on the
# enhancer's line ordering.
WORKDIR /home/{self.pr.repo}

# `npm ci` rather than `npm install`: the repo ships package-lock.json, and ci
# installs exactly what is locked. install would resolve fresh, so the three graded
# stages could each get different dependency versions and the f2p diff would be
# measuring dependency drift rather than the patch.
#
# --ignore-scripts is deliberate. Postinstall hooks in a JS tree routinely exec a
# binary npm is still writing (ETXTBSY on container overlayfs) or download a browser
# this image will never use. Nothing here needs them to run the test suite.
RUN npm ci --no-audit --no-fund --ignore-scripts

# Refuse to seal an image whose graded stages could not report anything. A missing
# jest or jest-junit yields an empty log, which reads downstream as "these tests do
# not exist" rather than as a broken image, and the harness scores that as a valid
# n2p-only resolve.
RUN npx jest --version > /dev/null
RUN node -e "require.resolve('jest-junit')" > /dev/null

{copy_commands}"""


@Instance.register("dorny", "test-reporter")
class TestReporter(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Only the JUnit XML is parsed, never jest's console output. In the console a
        # test name is indented beneath its describe block and only the FILE sits at
        # the margin, so parsing it yields file-level names that collapse every test
        # in a file into one entry - a single failure would then mask every pass
        # beside it.
        log = re.sub(r"\x1B\[[0-?]*[ -/]*[@-~]", "", log)

        # Attribute ORDER must not matter: jest-junit emits classname before name,
        # other runners emit name first. A regex that hardcodes one order silently
        # drops every testcase from the other, which reads downstream as "those tests
        # do not exist" rather than as a parse failure.
        testcase_re = re.compile(r"<testcase\b([^>]*?)(/>|>(.*?)</testcase>)", re.DOTALL)
        attr_re = re.compile(r'\b(name|classname)="([^"]*)"')

        def unescape(s: str) -> str:
            # &amp; LAST, or "&amp;lt;" would be unescaped twice into "<".
            for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&apos;", "'"), ("&amp;", "&")):
                s = s.replace(a, b)
            return s

        for m in testcase_re.finditer(log):
            attrs = dict(attr_re.findall(m.group(1)))
            name = unescape(attrs.get("name", ""))
            classname = unescape(attrs.get("classname", ""))
            if not name and not classname:
                continue
            closing, inner = m.group(2), m.group(3) or ""
            test_id = f"{classname}.{name}" if classname else name

            if closing == "/>":
                passed_tests.add(test_id)
            elif "<failure" in inner or "<error" in inner:
                failed_tests.add(test_id)
            elif "<skipped" in inner:
                skipped_tests.add(test_id)
            else:
                passed_tests.add(test_id)

        # Fallback: jest's verbose console output, used only when no JUnit XML was
        # captured. jest.config.js sets verbose: true, so every test is printed with
        # its own status marker:
        #
        #   PASS __tests__/jest-junit.test.ts
        #     jest-junit tests
        #       + report includes the short summary (3 ms)
        #
        # The file comes from the PASS/FAIL header and the describe chain is rebuilt
        # from indentation, so the id is file::describe > test - unique even when two
        # describes in one file share a test title. Timing is stripped because it
        # varies between stages, and a name that changes across stages appears as two
        # separate tests, inventing a transition that never happened.
        if not (passed_tests or failed_tests or skipped_tests):
            current_file = ""
            stack: list[tuple[int, str]] = []
            head_re = re.compile(r"^(PASS|FAIL)\s+(\S+\.(?:test|spec)\.tsx?)\s*$")
            case_re = re.compile(r"^(\s+)([✓✕×✗○✘])\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
            desc_re = re.compile(r"^(\s+)([^\s✓✕×✗○✘].*?)\s*$")

            for raw in log.split("\n"):
                line = raw.rstrip()
                m = head_re.match(line.strip())
                if m:
                    current_file = m.group(2)
                    stack = []
                    continue
                if not current_file:
                    continue

                m = case_re.match(line)
                if m:
                    indent, marker, title = len(m.group(1)), m.group(2), m.group(3).strip()
                    ancestors = [t for i, t in stack if i < indent]
                    path = " > ".join(ancestors + [title])
                    test_id = f"{current_file}::{path}"
                    if marker in ("✓",):
                        passed_tests.add(test_id)
                    elif marker in ("○",):
                        skipped_tests.add(test_id)
                    else:
                        failed_tests.add(test_id)
                    continue

                m = desc_re.match(line)
                if m:
                    indent, title = len(m.group(1)), m.group(2).strip()
                    stack = [(i, t) for i, t in stack if i < indent]
                    stack.append((indent, title))

        # A retried test can be reported twice. Enforce one bucket per test, or the
        # stage comparison double-counts and invents transitions.
        failed_tests -= passed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
