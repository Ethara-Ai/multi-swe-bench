import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

ORG = "redwoodjs"
REPO = "graphql"


_NODE_IMAGE = "node:18-bullseye"
_BASE_TAG = "base-multi-node"

_PACKAGES = [
    "ca-certificates",
    "curl",
    "build-essential",
    "git",
    "gnupg",
    "make",
    "python3",
    "sudo",
    "wget",
]

# The four PRs' CI matrices: node 14 (2020/21), node 16 (2023), node 18
# (2024/25). /opt/node14 carries npm-installed yarn 1; /opt/node16 and the
# image default carry corepack shims with the pinned berry versions cached.
# The build uses the classic builder, which does not populate TARGETARCH, so
# the architecture comes from dpkg (amd64 mapped to Node's x64 tarball name).
_TOOLCHAIN_SETUP = """RUN set -eux; \\
    corepack enable; \\
    export COREPACK_INTEGRITY_KEYS=0; \\
    corepack prepare yarn@3.6.3 --activate; \\
    arch="$(dpkg --print-architecture)"; \\
    [ "$arch" = amd64 ] && arch=x64; \\
    curl -fsSL "https://nodejs.org/dist/v14.21.3/node-v14.21.3-linux-$arch.tar.xz" -o /tmp/node14.tar.xz; \\
    mkdir -p /opt/node14; \\
    tar -xJf /tmp/node14.tar.xz -C /opt/node14 --strip-components=1; \\
    rm -f /tmp/node14.tar.xz; \\
    curl -fsSL "https://nodejs.org/dist/v16.20.2/node-v16.20.2-linux-$arch.tar.xz" -o /tmp/node16.tar.xz; \\
    mkdir -p /opt/node16; \\
    tar -xJf /tmp/node16.tar.xz -C /opt/node16 --strip-components=1; \\
    rm -f /tmp/node16.tar.xz; \\
    /opt/node14/bin/node /opt/node14/bin/npm install -g --prefix /opt/node14 yarn@1.22.19; \\
    /opt/node16/bin/node /opt/node16/bin/corepack enable; \\
    /opt/node16/bin/node /opt/node16/bin/corepack prepare yarn@3.0.2 --activate; \\
    /opt/node14/bin/node --version; \\
    /opt/node16/bin/node --version"""


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

# Exported by prepare.sh and every run script. Free of braces so it can be
# dropped into the f-string templates below.
RUN_ENV = """export CI=true
export NODE_OPTIONS="--max-old-space-size=4096"
export NO_COLOR=1
export FORCE_COLOR=0
export TERM=dumb
export DO_NOT_TRACK=1"""

# GitHub disabled the unauthenticated git:// protocol in 2022; the era-A
# yarn.lock still pins git:// dependency URLs.
GIT_URL_REWRITE = 'git config --global url."https://github.com/".insteadOf "git://github.com/"'


def stage_plan(pr_number: int) -> tuple[str, str, str, str]:
    """(node_prefix, install_cmd, package_dir, jest_args) for this PR's era.

    node_prefix is the PATH entry that puts the era's node+yarn first; the
    empty string keeps the base image's own node 18 + corepack yarn 3.6.3.
    """
    if pr_number == 9303:
        return (
            "",
            "yarn install",
            "packages/router",
            "--selectProjects code --runInBand --verbose --silent",
        )
    if pr_number == 4035:
        return (
            "/opt/node16/bin",
            "YARN_ENABLE_IMMUTABLE_INSTALLS=false yarn install",
            "packages/cli",
            "--runInBand --verbose --silent",
        )
    if pr_number == 987:
        return (
            "/opt/node14/bin",
            "yarn install --ignore-engines",
            "packages/structure",
            "--runInBand --verbose --silent",
        )
    return (
        "/opt/node14/bin",
        "yarn install --ignore-engines",
        "packages/cli",
        "--runInBand --verbose --silent",
    )


def node_prefix_export(pr_number: int) -> str:
    """PATH export putting this PR's era runtime first, if it is not the
    base image's own node 18."""
    prefix = stage_plan(pr_number)[0]
    if prefix:
        return f"export PATH={prefix}:$PATH"
    return "export PATH=/usr/local/bin:$PATH"


# ---------------------------------------------------------------- log parsing

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# jest --verbose: "PASS src/lib/__tests__/x.test.js" opens a file block.
# Multi-project runs insert a displayName ("PASS code src/...") and both forms
# can carry a timing suffix ("(37.183 s)"); the file path is what matters.
_SUITE = re.compile(
    r"^(PASS|FAIL)\s+(?:\S+\s+)?(\S+\.(?:ts|tsx|js|jsx|mjs|cjs))\s*(?:\(\d[^)]*s\))?$"
)

# One line per test: "  ✓ name (12 ms)". Timing suffix and leading mark vary.
_RESULT = re.compile(r"^(✓|✕|○|√|×)\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$")

# Summary/console lines that must never be mistaken for describe groups.
_NOISE = re.compile(
    r"^(Test Suites|Tests|Snapshots|Time|Ran all|Watch Usage|Babel|Jest|●|PASS|FAIL|Console|error|warning|node:|yarn|at )"
)

_PASS_MARKS = ("✓", "√")
_FAIL_MARKS = ("✕", "×")


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
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_img = self.dependency()
        packages_str = " \\\n    ".join(_PACKAGES)
        apt_command = self._get_apt_update_command(packages_str, base_img)
        # Bullseye's archived security suite 404s on install (index newer
        # than the pruned pool); main + updates still serve everything.
        apt_command = apt_command.replace(
            "RUN apt-get update",
            "RUN sed -i '/debian-security/d' /etc/apt/sources.list || true && apt-get update",
            1,
        )

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
    COREPACK_INTEGRITY_KEYS=0 \\
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

{apt_command}

{_TOOLCHAIN_SETUP}

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
        _, install_cmd, package_dir, jest_args = stage_plan(self.pr.number)
        prefix_export = node_prefix_export(self.pr.number)

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

{prefix_export}
{RUN_ENV}

node --version
yarn --version

# Install the union of dependencies needed by the final (fix) state: apply
# both patches, install, then restore the pristine base tree. node_modules
# survives the reset, so fix-stage dependencies are present.
{GIT_URL_REWRITE}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{install_cmd}
git checkout -- .
git clean -fd
git status --porcelain

# Build all workspace packages at the base commit (CI parity: install -> build -> test).
yarn build

# Smoke test: enumerate the target package's suite without running it.
cd /home/{REPO}/{package_dir}
yarn jest --listTests
""",
            ),
            File(
                ".",
                "run.sh",
                f"""{SHEBANG}

cd /home/{REPO}
{prefix_export}
{RUN_ENV}

cd {package_dir}
yarn jest {jest_args} || true
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""{SHEBANG}

cd /home/{REPO}
{prefix_export}
{RUN_ENV}

{APPLY_TEST}

cd {package_dir}
yarn jest {jest_args} || true
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""{SHEBANG}

cd /home/{REPO}
{prefix_export}
{RUN_ENV}

{APPLY_FIX}

cd {package_dir}
yarn jest {jest_args} || true
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
class RedwoodGraphql(Instance):
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

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        current_file = ""
        groups: list[str] = []
        in_details = False

        for raw_line in test_log.splitlines():
            line = ANSI_ESCAPE.sub("", raw_line).rstrip("\r")
            stripped = line.strip()
            if not stripped:
                continue

            suite_match = _SUITE.match(stripped)
            if suite_match:
                current_file = suite_match.group(2)
                groups = []
                in_details = False
                continue

            if stripped.startswith("●"):
                in_details = True
                continue

            test_match = _RESULT.match(stripped)
            if test_match:
                mark = test_match.group(1)
                name = test_match.group(2)
                parts = []
                if current_file:
                    parts.append(current_file)
                parts.extend(groups)
                parts.append(name)
                full_name = " > ".join(parts)
                if mark in _PASS_MARKS:
                    if full_name not in failed_tests:
                        passed_tests.add(full_name)
                elif mark in _FAIL_MARKS:
                    if full_name in passed_tests:
                        passed_tests.remove(full_name)
                    failed_tests.add(full_name)
                else:
                    if full_name not in failed_tests:
                        skipped_tests.add(full_name)
                continue

            if in_details:
                continue

            if _NOISE.match(stripped):
                continue

            # Describe group: indented prose above its tests, without stack
            # frames (at ...) or file:line:col references. The browserslist
            # update banner is indented like a group but is stdout noise.
            indent = len(line) - len(line.lstrip(" "))
            if indent <= 0:
                continue
            if "browserslist" in stripped.lower():
                continue
            if " at " in stripped or re.search(r":\d+:\d+", stripped):
                continue

            level = indent // 2
            if level > len(groups):
                groups.append(stripped)
            else:
                groups = groups[: level - 1] + [stripped]

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
