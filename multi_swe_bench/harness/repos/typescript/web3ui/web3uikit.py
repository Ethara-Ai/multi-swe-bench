import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "web3ui"
REPO = "web3uikit"


_NODE_IMAGE = "node:16-bullseye"
_BASE_TAG = "base-node16-yarn-tsdx"


# --------------------------------------------------------------------- scripts

SHEBANG = "#!/bin/bash\nset -eo pipefail"

CHECK_GIT_CHANGES = """#!/bin/bash
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

APPLY_TEST = """if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

APPLY_FIX = """if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi"""

# Exported by every run script and by prepare.sh. Free of braces so it can be
# dropped into the f-string templates below.
RUN_ENV = """export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export DO_NOT_TRACK=1"""

# yarn 1 lockfile at every commit makes --frozen-lockfile the reproducible
# install; fall back to an unlocked install only if the lock drifted.
INSTALL = (
    "yarn install --frozen-lockfile --network-timeout 600000 "
    "|| yarn install --network-timeout 600000"
)

# tsdx test compiles via ts-jest on the fly -- no build step exists. This
# cache-warm run primes jest's transform/haste caches at build time and must
# tolerate failures (tests failing at the base sha is normal for f2p PRs).
CACHE_WARM = "yarn tsdx test --runInBand --silent || true"

# R3: byte-identical in run.sh / test-run.sh / fix-run.sh. R14 --runInBand (no
# worker pool), R15 no `--` separator behind yarn. `|| true` keeps the log
# complete through failing suites; the exit code is never the signal, the
# parsed log is.
TEST_CMD = "yarn tsdx test --runInBand --verbose || true"


# ---------------------------------------------------------------- log parsing

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# "PASS src/components/Icon/Icon.test.tsx" / "FAIL src/... (5.889s)"
_FILE_LINE = re.compile(
    r"^(PASS|FAIL)\s+(?P<file>\S+?)(?:\s+\(\d+(?:\.\d+)?m?s\))?\s*$"
)

# "    ✓ renders the component (28ms)" / "  ✕ name (1.2s)" /
# "    ○ skipped renders the correct size". Jest prints the mark at
# (describe-depth + 1) * 2 spaces; the indent drives the describe stack.
_TEST_LINE = re.compile(
    r"^(?P<indent>\s+)(?P<mark>\u2713|\u2715|\u25cb\s+skipped)\s+"
    r"(?P<name>.+?)"
    r"(?:\s*\(\s*\d+(?:\.\d+)?(?:ms|s|m)?(?:\s+\d+(?:\.\d+)?(?:ms|s|m))?\s*\))?$"
)

# Everything after these lines belongs to failure details / console output /
# summaries, not the per-suite tree. jest 26 reprints failing suites after
# "Summary of all failing tests" with ● headers only, so switching the tree off
# here is what keeps a test from being counted twice.
_TREE_END = re.compile(
    r"^\s*(\u25cf|console\.|Test Suites:|Tests:|Snapshots:|Time:|"
    r"Ran all test suites|at |yarn run|Done in)"
)

_PASS_MARK = "\u2713"
_FAIL_MARK = "\u2715"
_SKIP_MARK = "\u25cb"


# --------------------------------------------------------------------- images

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
        return _NODE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # The leading syntax directive keeps DockerfileEnhancer from rewriting
        # the clone or pinning this shared image to one PR's BASE_COMMIT, so the
        # infrastructure block it would otherwise contribute is written out here.
        # BASE_COMMIT is declared but deliberately unused: build_dataset passes
        # it to every image whose dependency() is a str, and an undeclared build
        # arg is a build warning.
        return f"""# syntax=docker/dockerfile:1.6

FROM {base_img}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{ORG}/{REPO}.git"
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
    CI=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{ORG}/{REPO}" \\
      org.opencontainers.image.description="{ORG}/{REPO} Docker image" \\
      org.opencontainers.image.source="https://github.com/{ORG}/{REPO}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/ca-bundle.crt
{global_env}
WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{REPO}
{clear_env}
CMD ["/bin/bash"]
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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                f"""{SHEBANG}
cd /home/{REPO}
bash /home/check_git_changes.sh
{RUN_ENV}
{INSTALL}
{CACHE_WARM}
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{RUN_ENV}
{TEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_TEST}
{RUN_ENV}
{TEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}
cd /home/{REPO}
{APPLY_FIX}
{RUN_ENV}
{TEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()

        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())

        global_env = f"\n{self.global_env}\n" if self.global_env else ""
        clear_env = f"\n{self.clear_env}\n" if self.clear_env else ""

        # Chains to a base Image rather than a str, so DockerfileEnhancer returns
        # this verbatim and injects nothing; the hardening block is applied by
        # hand. It opens with `git checkout --detach "${BASE_COMMIT}"`, so it
        # performs this PR's checkout as well as pruning the full history
        # inherited from the shared base. The repo is cloned once in that base,
        # so this layer never clones and needs no REPO_URL.
        return f"""FROM {base.image_full_name()}

ARG BASE_COMMIT={self.pr.base.sha}
{global_env}
{copy_commands}
WORKDIR /home/{REPO}

{Image._HARDENING_BLOCK.rstrip()}

RUN bash /home/prepare.sh
{clear_env}"""


# ------------------------------------------------------------------- instance

@Instance.register(ORG, REPO)
class Web3uikit(Instance):
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

    def parse_log(self, log: str) -> TestResult:
        passed: set[str] = set()
        failed: set[str] = set()
        skipped: set[str] = set()

        current_file = ""
        in_tree = False
        describe_stack: list[tuple[int, str]] = []

        for line in ANSI_ESCAPE.sub("", log).splitlines():
            if not line.strip():
                continue

            file_match = _FILE_LINE.match(line)
            if file_match:
                current_file = file_match.group("file")
                in_tree = True
                describe_stack = []
                continue

            if not in_tree or not current_file:
                continue

            if _TREE_END.match(line):
                in_tree = False
                describe_stack = []
                continue

            test_match = _TEST_LINE.match(line)
            if test_match:
                indent = len(test_match.group("indent"))
                name = test_match.group("name").strip()
                while describe_stack and describe_stack[-1][0] >= indent:
                    describe_stack.pop()
                full_name = " > ".join([d[1] for d in describe_stack] + [name])
                full_name = f"{current_file} > {full_name}"

                mark = test_match.group("mark")
                if mark == _PASS_MARK:
                    passed.add(full_name)
                elif mark == _FAIL_MARK:
                    failed.add(full_name)
                else:
                    skipped.add(full_name)
                continue

            # Describe header: indented text inside the suite tree at an even
            # indent (jest indents 2 spaces per describe level).
            stripped = line.strip()
            if line[0] == " " and not stripped.startswith("at "):
                indent = len(line) - len(line.lstrip())
                if indent >= 2 and indent % 2 == 0:
                    while describe_stack and describe_stack[-1][0] >= indent:
                        describe_stack.pop()
                    describe_stack.append((indent, stripped))

        # A retried test can be reported twice with different marks. Worst wins:
        # TestResult.__post_init__ rejects any overlap between the three sets.
        passed -= failed
        skipped -= failed
        skipped -= passed

        return TestResult(
            passed_count=len(passed),
            failed_count=len(failed),
            skipped_count=len(skipped),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
        )
