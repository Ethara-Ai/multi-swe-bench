import re
from typing import Optional

from multi_swe_bench.harness.image import (
    Config,
    DockerfileEnhancer,
    File,
    Image,
    _safe_path_component,
)
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_TEST_PATCH_FILE_RE = re.compile(r"^diff --git a/\S+ b/tests/(\S+\.test\.cjs)$", re.M)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_TAP_SUBTEST_RE = re.compile(r"^(?P<indent> *)# Subtest: (?P<name>.*)$")
_TAP_RESULT_RE = re.compile(
    r"^(?P<indent> *)(?P<status>ok|not ok) \d+ - "
    r"(?P<name>.*?)(?: # (?P<directive>SKIP|TODO)\b.*)?$"
)
_TAP_TYPE_RE = re.compile(r"^\s*type: '(?P<kind>test|suite)'\s*$")

_SPEC_SUITE_RE = re.compile(r"^(?P<indent> *)▶ (?P<name>.*)$")
_SPEC_RESULT_RE = re.compile(
    r"^(?P<indent> *)(?P<glyph>[✔✖﹣]) "
    r"(?P<name>.*?) \([0-9.]+ms\)(?P<trailer>.*)$"
)
_SPEC_SUMMARY_RE = re.compile(r"^ℹ \w+ ")

_TEST_COMMAND = 'node scripts/run-tests.cjs --files "$SELECTED"'

_BUILD_COMMAND = "npm run build:lib"


def _test_patch_files(test_patch: Optional[str]) -> list[str]:
    selected: list[str] = []
    for name in _TEST_PATCH_FILE_RE.findall(test_patch or ""):
        if name not in selected:
            selected.append(name)
    return selected


def _test_block(pr: PullRequest) -> str:
    files = " ".join(_test_patch_files(pr.test_patch))
    return f"""{_BUILD_COMMAND}

SELECTED=""
for f in {files}; do
    if [ -f "tests/$f" ]; then
        if [ -z "$SELECTED" ]; then
            SELECTED="$f"
        else
            SELECTED="$SELECTED $f"
        fi
    fi
done

if [ -n "$SELECTED" ]; then
    {_TEST_COMMAND}
else
    echo "NO_SELECTED_TESTS"
fi"""


class GsdCoreImageBase(Image):
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
        return "node:24-bookworm-slim"

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        repo = _safe_path_component(self.pr.repo)
        repo_url = f"https://github.com/{self.pr.org}/{self.pr.repo}.git"

        return f"""{DockerfileEnhancer.SYNTAX_DIRECTIVE}

FROM {self.dependency()}

ARG TARGETARCH
ARG REPO_URL="{repo_url}"
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
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
    NODE_EXTRA_CA_CERTS=${{CA_CERT_PATH}}

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

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
        bash \\
        ca-certificates \\
        coreutils \\
        curl \\
        git \\
        jq && \\
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{repo}

CMD ["/bin/bash"]
"""


class GsdCoreImageDefault(Image):
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
        return GsdCoreImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def dockerfile(self) -> str:
        repo = _safe_path_component(self.pr.repo)
        hardening = Image._HARDENING_BLOCK.rstrip("\n")

        return f"""FROM {self.dependency().image_full_name()}

ARG BASE_COMMIT="{self.pr.base.sha}"

COPY fix.patch /home/
COPY test.patch /home/
COPY check_git_changes.sh /home/
COPY prepare.sh /home/
COPY run.sh /home/
COPY test-run.sh /home/
COPY fix-run.sh /home/

RUN bash /home/prepare.sh

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{hardening}
"""

    def files(self) -> list[File]:
        repo = _safe_path_component(self.pr.repo)
        test_block = _test_block(self.pr)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

cd /home/{repo}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh

npm ci || true
npm run build:lib || true

node -e "require.resolve('typescript/bin/tsc')"
node -e "require.resolve('./scripts/run-tests.cjs')"
npm run build:lib
TRACKED=$(git ls-files -- gsd-core/bin/lib | grep -c '\\.cjs$')
EMITTED=$(find gsd-core/bin/lib -name '*.cjs' -type f | wc -l)
test "$EMITTED" -gt "$TRACKED"
test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

{test_block}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

{test_block}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{test_block}
""",
            ),
        ]


@Instance.register("open-gsd", "gsd-core")
class GsdCore(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GsdCoreImageDefault(self.pr, self._config)

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
        log = _ANSI_RE.sub("", test_log)
        lines = log.splitlines()

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        if any(_TAP_RESULT_RE.match(line) for line in lines):
            self._parse_tap(lines, passed_tests, failed_tests, skipped_tests)
        else:
            self._parse_spec(lines, passed_tests, failed_tests, skipped_tests)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )

    @staticmethod
    def _unescape_tap(name: str) -> str:
        return name.replace("\\#", "#").replace("\\\\", "\\")

    @staticmethod
    def _qualify(stack: list[tuple[int, str]], indent: int, name: str) -> str:
        prefix = [entry for depth, entry in stack if depth < indent]
        return " > ".join(prefix + [name])

    @staticmethod
    def _push(stack: list[tuple[int, str]], indent: int, name: str) -> None:
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, name))

    def _parse_tap(
        self,
        lines: list[str],
        passed_tests: set[str],
        failed_tests: set[str],
        skipped_tests: set[str],
    ) -> None:
        stack: list[tuple[int, str]] = []

        for index, line in enumerate(lines):
            subtest = _TAP_SUBTEST_RE.match(line)
            if subtest:
                self._push(
                    stack,
                    len(subtest.group("indent")),
                    self._unescape_tap(subtest.group("name")),
                )
                continue

            result = _TAP_RESULT_RE.match(line)
            if not result:
                continue

            kind = "test"
            for lookahead in lines[index + 1 : index + 16]:
                type_match = _TAP_TYPE_RE.match(lookahead)
                if type_match:
                    kind = type_match.group("kind")
                    break
                if _TAP_RESULT_RE.match(lookahead) or _TAP_SUBTEST_RE.match(lookahead):
                    break
            if kind == "suite":
                continue

            indent = len(result.group("indent"))
            name = self._qualify(
                stack, indent, self._unescape_tap(result.group("name"))
            )
            directive = result.group("directive")

            if directive:
                skipped_tests.add(name)
            elif result.group("status") == "ok":
                passed_tests.add(name)
            else:
                failed_tests.add(name)

    def _parse_spec(
        self,
        lines: list[str],
        passed_tests: set[str],
        failed_tests: set[str],
        skipped_tests: set[str],
    ) -> None:
        stack: list[tuple[int, str]] = []
        announced: set[tuple[int, str]] = set()
        in_summary = False

        for line in lines:
            if _SPEC_SUMMARY_RE.match(line):
                in_summary = True
                continue

            suite = _SPEC_SUITE_RE.match(line)
            if suite:
                in_summary = False
                indent = len(suite.group("indent"))
                name = suite.group("name")
                announced.add((indent, name))
                self._push(stack, indent, name)
                continue

            result = _SPEC_RESULT_RE.match(line)
            if not result or in_summary:
                continue

            indent = len(result.group("indent"))
            raw_name = result.group("name")
            if (indent, raw_name) in announced:
                continue

            name = self._qualify(stack, indent, raw_name)
            glyph = result.group("glyph")
            trailer = result.group("trailer").strip()

            if glyph == "﹣":
                skipped_tests.add(name)
            elif glyph == "✖":
                failed_tests.add(name)
            elif trailer.startswith("#"):
                skipped_tests.add(name)
            else:
                passed_tests.add(name)
