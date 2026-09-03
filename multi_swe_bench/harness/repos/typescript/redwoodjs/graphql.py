import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_YARN_BERRY_FIRST_PR = 3000

# One shared base image for the whole repo config. It carries only the
# toolchain -- it deliberately does NOT clone the repository. The clone,
# the checkout of the PR's base commit and the history scrub all happen
# inside prepare.sh's single RUN in the per-PR layer, so no lower image
# layer ever retains unscrubbed history that could leak future commits.
_SHARED_BASE_IMAGE = "node:16-bullseye"
_BASE_IMAGE_TAG = "base"

# PRs predating yarn berry were developed on Node 14 with yarn classic.
# The shared base ships that toolchain alongside Node 16 so a single base
# image can serve both eras without changing either PR's runtime.
_LEGACY_NODE_VERSION = "14.21.3"
_LEGACY_YARN_VERSION = "1.22.19"
_LEGACY_NODE_PREFIX = "/opt/node14"

_TARGET_PACKAGES: dict[int, tuple[str, ...]] = {
    1854: ("packages/cli",),
    3536: ("packages/cli",),
    3598: ("packages/cli",),
    3616: ("packages/cli",),
    3772: ("packages/router", "packages/testing"),
}

_DEFAULT_TARGET_PACKAGES: tuple[str, ...] = ("packages/cli",)

_JSON_BEGIN = "===MSWEB_JEST_JSON_BEGIN==="
_JSON_END = "===MSWEB_JEST_JSON_END==="

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_JSON_BLOCK = re.compile(
    re.escape(_JSON_BEGIN) + r"\s*\n(.*?)\n\s*" + re.escape(_JSON_END),
    re.DOTALL,
)

_APT_PACKAGES = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    ca-certificates \\\n"
    "    curl \\\n"
    "    build-essential \\\n"
    "    git \\\n"
    "    gnupg \\\n"
    "    make \\\n"
    "    python3 \\\n"
    "    sudo \\\n"
    "    wget \\\n"
    "    xz-utils \\\n"
    "    && rm -rf /var/lib/apt/lists/*"
)

_LEGACY_NODE_SETUP = f"""RUN set -eux; \\
    arch="$(dpkg --print-architecture)"; \\
    case "$arch" in \\
        amd64) node_arch=x64 ;; \\
        arm64) node_arch=arm64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    mkdir -p {_LEGACY_NODE_PREFIX}; \\
    curl -fsSL "https://nodejs.org/dist/v{_LEGACY_NODE_VERSION}/node-v{_LEGACY_NODE_VERSION}-linux-$node_arch.tar.xz" \\
        | tar -xJ -C {_LEGACY_NODE_PREFIX} --strip-components=1; \\
    {_LEGACY_NODE_PREFIX}/bin/npm install -g --prefix {_LEGACY_NODE_PREFIX} yarn@{_LEGACY_YARN_VERSION}; \\
    {_LEGACY_NODE_PREFIX}/bin/node --version; \\
    {_LEGACY_NODE_PREFIX}/bin/yarn --version"""


def _uses_yarn_berry(number: int) -> bool:
    return number >= _YARN_BERRY_FIRST_PR


def _uses_legacy_node(number: int) -> bool:
    return not _uses_yarn_berry(number)


def _target_packages(number: int) -> tuple[str, ...]:
    return _TARGET_PACKAGES.get(number, _DEFAULT_TARGET_PACKAGES)


def _stage_env(number: int) -> str:
    lines = [
        "export CI=true",
        'export NODE_OPTIONS="--max-old-space-size=4096"',
    ]
    if _uses_legacy_node(number):
        # Select the Node 14 / yarn classic toolchain baked into the shared base.
        lines.append(f'export PATH="{_LEGACY_NODE_PREFIX}/bin:$PATH"')
    if _uses_yarn_berry(number):
        lines.append("export YARN_ENABLE_IMMUTABLE_INSTALLS=false")
        lines.append("export YARN_NODE_LINKER=node-modules")
        lines.append("export YARN_HTTP_TIMEOUT=600000")
    return "\n".join(lines)


def _install_command(number: int) -> str:
    if _uses_yarn_berry(number):
        return "yarn install"
    return "yarn install --frozen-lockfile"

def _test_body(pr: PullRequest) -> str:
    packages = " ".join(_target_packages(pr.number))
    return f"""
PACKAGES="{packages}"

for pkg in $PACKAGES; do
    cd /home/{pr.repo}/$pkg
    set +e
    yarn build:js
    BUILD_RC=$?
    set -e
    if [ "$BUILD_RC" -ne 0 ]; then
        echo "NOTE: yarn build:js exited $BUILD_RC in $pkg; the suite runs against the previously built output"
    fi
done

for pkg in $PACKAGES; do
    cd /home/{pr.repo}/$pkg
    REPORT="/tmp/jest-$(echo $pkg | tr / _).json"
    rm -f "$REPORT"
    set +e
    yarn jest src --json --outputFile="$REPORT" --colors=false --maxWorkers=2
    JEST_RC=$?
    set -e
    if [ ! -s "$REPORT" ]; then
        echo "Error: jest wrote no report for $pkg (exit $JEST_RC)" >&2
        exit 1
    fi
    echo "{_JSON_BEGIN}"
    cat "$REPORT"
    echo
    echo "{_JSON_END}"
done
"""


def parse_jest_json_log(log: str, repo: str) -> TestResult:
    clean = _ANSI_ESCAPE.sub("", log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()
    occurrences: dict[str, int] = {}

    prefix = f"/home/{repo}/"

    for block in _JSON_BLOCK.findall(clean):
        try:
            report = json.loads(block)
        except ValueError:
            continue

        for suite in report.get("testResults") or []:
            path = (suite.get("name") or "").replace("\\", "/")
            if path.startswith(prefix):
                path = path[len(prefix) :]

            assertions = suite.get("assertionResults") or []
            if not assertions:
                if suite.get("status") == "failed":
                    failed_tests.add(path)
                continue

            for assertion in assertions:
                titles = [t for t in (assertion.get("ancestorTitles") or []) if t]
                name = " > ".join([path] + titles + [assertion.get("title") or ""])

                occurrences[name] = occurrences.get(name, 0) + 1
                if occurrences[name] > 1:
                    name = f"{name} #{occurrences[name]}"

                status = assertion.get("status")
                if status == "passed":
                    passed_tests.add(name)
                elif status == "failed":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)

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



class RedwoodjsGraphqlImageBase(Image):
    """Single shared base image for every PR in this repo config.

    Deliberately toolchain-only: no clone, no per-PR checkout, no history
    scrub. Those cannot live here because one image tag cannot hold five
    different base commits, and because anything cloned here would persist
    in a lower layer of every PR image even if a later layer scrubbed it.
    """

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> str | Image:
        return _SHARED_BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_IMAGE_TAG

    def workdir(self) -> str:
        return _BASE_IMAGE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()

        sections = [f"FROM {base_image}"]
        if self.global_env:
            sections.append(self.global_env)
        sections.append(
            "WORKDIR /home/\nENV DEBIAN_FRONTEND=noninteractive\nENV LANG=C.UTF-8"
        )
        sections.append(_APT_PACKAGES)
        sections.append(_LEGACY_NODE_SETUP)
        if self.clear_env:
            sections.append(self.clear_env)
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(sections) + "\n"


_PREPARE_TEMPLATE = """#!/bin/bash
set -e
{env}

git config --global --add safe.directory /home/{repo}
git config --global url."https://github.com/".insteadOf "git://github.com/"

# Clone, pin to the PR's base commit and scrub the history in ONE layer.
# Doing all three here (rather than in the shared base image) is what keeps
# future commits out of the shipped image: no lower layer ever holds the
# unscrubbed clone, so the fix cannot be recovered from image history.
# The packfile transfer is ~500MB and dies with "early EOF"/"index-pack failed"
# on a lossy link, so tolerate slow/interrupted transfers and retry the clone.
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 1000
git config --global http.lowSpeedTime 300

for attempt in 1 2 3 4 5; do
    rm -rf /home/{repo}
    if git clone "{repo_url}" /home/{repo}; then
        break
    fi
    echo "clone attempt $attempt failed; retrying in 20s" >&2
    sleep 20
done
if [ ! -d /home/{repo}/.git ]; then
    echo "FATAL: git clone failed after 5 attempts" >&2
    exit 1
fi
cd /home/{repo}

git checkout --detach {sha}
git remote remove origin 2>/dev/null || true
git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
    | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all
git reflog expire --expire-unreachable=now --all
git gc --prune=now --aggressive
git repack -a -d -l --quiet
rm -f .git/objects/info/alternates
git config --local gc.auto 0
git config --local fetch.recurseSubmodules false
git config --local remote.pushDefault ""

# Integrity asserts: pinned to the right commit, and nothing else is reachable.
test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"
test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"
test -z "$(git remote)"
test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

if [ -f .gitmodules ]; then
    git submodule foreach --recursive '
        git checkout --detach HEAD;
        git remote remove origin 2>/dev/null || true;
        git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
            | xargs -r -n1 git update-ref -d;
        git reflog expire --expire=now --all;
        git reflog expire --expire-unreachable=now --all;
        git gc --prune=now --aggressive;
        rm -f .git/objects/info/alternates;
    '
fi

git reset --hard
bash /home/check_git_changes.sh

# Retry the install: transient DNS/TLS failures otherwise leave node_modules
# incomplete, and the trailing "|| true" would hide that behind a green build.
for attempt in 1 2 3; do
    if {install}; then
        break
    fi
    echo "install attempt $attempt failed; retrying in 20s" >&2
    sleep 20
done
yarn build || true
"""


class RedwoodjsGraphqlImageDefault(Image):

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
        return RedwoodjsGraphqlImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
                _PREPARE_TEMPLATE.format(
                    repo=self.pr.repo,
                    repo_url=f"https://github.com/{self.pr.org}/{self.pr.repo}.git",
                    sha=self.pr.base.sha,
                    env=_stage_env(self.pr.number),
                    install=_install_command(self.pr.number),
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
{env}

cd /home/{pr.repo}
""".format(pr=self.pr, env=_stage_env(self.pr.number))
                + _test_body(self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
{env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed for test.patch" >&2
    exit 1
fi
""".format(pr=self.pr, env=_stage_env(self.pr.number))
                + _test_body(self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
{env}

cd /home/{pr.repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed for test.patch and fix.patch" >&2
    exit 1
fi
""".format(pr=self.pr, env=_stage_env(self.pr.number))
                + _test_body(self.pr),
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

@Instance.register("redwoodjs", "graphql")
class RedwoodjsGraphql(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return RedwoodjsGraphqlImageDefault(self.pr, self._config)

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
        return parse_jest_json_log(test_log, self.pr.repo)
