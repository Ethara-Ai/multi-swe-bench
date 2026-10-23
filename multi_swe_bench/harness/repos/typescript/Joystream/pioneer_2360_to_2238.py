import re
import json
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


_MITM_PROXY_ARGS = '''\
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'''

_MITM_ENV_BLOCK = '''\
ENV DEBIAN_FRONTEND=noninteractive \\
    LANG=C.UTF-8 \\
    LC_ALL=C.UTF-8 \\
    TZ=UTC \\
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}'''

_MITM_CERT_SYMLINKS = '''\
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''

CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

PREPARE_GIT_SH = """git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[BASE_SHA]]
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "[[BASE_SHA]]"
"""


class PioneerEraBase(Image):
    def __init__(
        self,
        pr: PullRequest,
        config: Config,
        node_image: str,
        interval_name: str,
    ):
        self._pr = pr
        self._config = config
        self._node_image = node_image
        self._interval_name = interval_name

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return self._node_image

    def image_tag(self) -> str:
        return f"base-{self._interval_name}"

    def workdir(self) -> str:
        return f"base-{self._interval_name}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        global_env = self.global_env.strip()
        clear_env = self.clear_env.strip()
        global_section = f"\n{global_env}\n" if global_env else ""
        clear_section = f"\n{clear_env}\n" if clear_env else ""

        return f"""# syntax=docker/dockerfile:1.6
FROM {self._node_image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"

{_MITM_PROXY_ARGS}

{_MITM_ENV_BLOCK}

{_MITM_CERT_SYMLINKS}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"
{global_section}
RUN if ! command -v git >/dev/null 2>&1 || [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then \\
        apt-get update && apt-get install -y --no-install-recommends git ca-certificates \\
        && rm -rf /var/lib/apt/lists/*; \\
    fi; \\
    git --version; \\
    test -s /etc/ssl/certs/ca-certificates.crt

RUN git config --global --add safe.directory '*'

WORKDIR /home/
RUN git clone "${{REPO_URL}}" /home/{repo} && git -C /home/{repo} rev-parse HEAD
WORKDIR /home/{repo}
{clear_section}
CMD ["/bin/bash"]
"""


def pioneer_pr_dockerfile(image: Image) -> str:
    base = image.dependency()
    repo = image.pr.repo
    sha = image.pr.base.sha
    early = ("check_git_changes.sh", "fix.patch", "prepare.sh")
    files = image.files()
    pre_copy = "".join(f"COPY {name} /home/\n" for name in early if any(f.name == name for f in files))
    post_copy = "".join(f"COPY {f.name} /home/\n" for f in files if f.name not in early)
    global_env = image.global_env.strip()
    clear_env = image.clear_env.strip()
    global_section = f"\n{global_env}\n" if global_env else ""
    clear_section = f"\n{clear_env}\n" if clear_env else ""

    return f"""FROM {base.image_name()}:{base.image_tag()}

ARG BASE_COMMIT="{sha}"
{global_section}
{pre_copy}
RUN bash /home/prepare.sh

{post_copy}
RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --quiet; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --quiet; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
{clear_section}"""


class ImageDefault_PIONEER_2360_TO_2238(Image):
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
        return PioneerEraBase(self.pr, self._config, "node:14-bullseye", "pioneer_2360_to_2238")

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_name = self.pr.repo
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
                CHECK_GIT_CHANGES_SH,
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -euo pipefail

cd /home/pioneer

""" + PREPARE_GIT_SH.replace("[[BASE_SHA]]", self.pr.base.sha) + """

BASE_HEAD="$(git rev-parse HEAD)"
echo "node_modules/" >> .git/info/exclude
if grep -qE '^diff --git a/(.*/)?(package[.]json|yarn[.]lock) ' /home/fix.patch; then
    git apply --whitespace=nowarn --include='*package.json' --include='yarn.lock' /home/fix.patch
    FIX_DEPS=1
fi

attempt=1
while :; do
    if yarn install --frozen-lockfile --network-timeout 600000 || yarn install --network-timeout 600000; then
        break
    fi
    if [ "$attempt" -ge 5 ]; then
        echo "yarn install failed after $attempt attempts" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep $((attempt * 15))
done

if [ "${FIX_DEPS:-0}" = 1 ]; then
    git checkout -- .
    git clean -fd
    bash /home/check_git_changes.sh
    test "$(git rev-parse HEAD)" = "$BASE_HEAD"
fi

yarn build

node --version
yarn --version
test -d node_modules
test -n "$(ls -A node_modules)"
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export NODE_ENV=test
cd /home/[[REPO_NAME]]
yarn workspace ui test --verbose --testTimeout=30000 --maxWorkers=2
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export NODE_ENV=test
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch; then
    echo "Strict apply failed, retrying with --3way" >&2
    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --3way /home/test.patch; then
        echo "Error: git apply failed" >&2
        exit 1
    fi
fi
yarn workspace ui test --verbose --testTimeout=30000 --maxWorkers=2
""".replace("[[REPO_NAME]]", repo_name),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export TZ=UTC
export NODE_ENV=test
cd /home/[[REPO_NAME]]
if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Strict apply failed, retrying with --3way" >&2
    if ! git -C /home/[[REPO_NAME]] apply --whitespace=nowarn --3way /home/test.patch /home/fix.patch; then
        echo "Error: git apply failed" >&2
        exit 1
    fi
fi
for pkg in types metadata-protobuf; do
    if [ -f packages/$pkg/package.json ] && grep -q "^diff --git a/packages/$pkg/" /home/fix.patch; then
        (cd packages/$pkg && yarn build) || echo "Warning: rebuild of packages/$pkg failed" >&2
    fi
done
yarn workspace ui test --verbose --testTimeout=30000 --maxWorkers=2
""".replace("[[REPO_NAME]]", repo_name),
            ),
        ]

    def dockerfile(self) -> str:
        return pioneer_pr_dockerfile(self)


@Instance.register("Joystream", "pioneer_2360_to_2238")
class PIONEER_2360_TO_2238(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ImageDefault_PIONEER_2360_TO_2238(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        log = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", log)

        line_re = re.compile(r"^(?:[^|\n]*\|)?(?P<indent>[ \t]*)(?P<body>.*?)\s*$")
        suite_re = re.compile(
            r"^(PASS|FAIL|SKIPPED)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)"
        )
        case_re = re.compile(
            r"^(?P<glyph>[\u2713\u2714\u2715\u2717\u00d7\u25cb\u26ac]|x)\s+"
            r"(?P<name>.+?)(?:\s+\(\d+(?:\.\d+)?\s*(?:ms|s)\))?$"
        )
        noise = (
            "Tests:",
            "Test Suites:",
            "Snapshots:",
            "Time:",
            "Ran ",
            "$ ",
            "at ",
            "console.",
            "Warning:",
            "yarn run",
            "npx ",
            "Browserslist:",
            "PASS",
            "FAIL",
            "SKIPPED",
        )

        suite = ""
        groups: list[str] = []
        for raw in log.split("\n"):
            m = line_re.match(raw.rstrip("\r"))
            if not m:
                continue
            body = m.group("body")
            if not body:
                continue
            indent = len(m.group("indent").expandtabs(2))

            suite_match = suite_re.match(body)
            if suite_match:
                suite = suite_match.group(2)
                groups = []
                if suite_match.group(1) == "PASS":
                    passed_tests.add(suite)
                elif suite_match.group(1) == "FAIL":
                    failed_tests.add(suite)
                continue
            if body.startswith("\u25cf"):
                continue

            case_match = case_re.match(body)
            if case_match:
                parts = [p for p in [suite, *groups, case_match.group("name").strip()] if p]
                name = " > ".join(parts)
                glyph = case_match.group("glyph")
                if glyph in ("\u2713", "\u2714"):
                    passed_tests.add(name)
                elif glyph in ("\u2715", "\u2717", "\u00d7", "x"):
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            if indent >= 2 and not body.startswith(noise):
                level = max(indent // 2 - 1, 0)
                groups = groups[:level] + [body]

        passed_tests -= failed_tests
        skipped_tests -= failed_tests | passed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


_PIONEER_ORG = "Joystream"
_PIONEER_REPO = "pioneer"
_PIONEER_INTERVAL_RE = re.compile(
    rf"^{_PIONEER_ORG}/{_PIONEER_REPO}_(\d+)_to_(\d+)$"
)


def _pioneer_registered_eras():
    eras = []
    for key, era_cls in Instance._registry.items():
        matched = _PIONEER_INTERVAL_RE.match(key)
        if not matched:
            continue
        first, second = int(matched.group(1)), int(matched.group(2))
        eras.append((min(first, second), max(first, second), era_cls))
    eras.sort(key=lambda era: (era[0], era[1]))
    return eras


def _pioneer_era_for(number: int):
    eras = _pioneer_registered_eras()
    if not eras:
        raise ValueError(
            f"no {_PIONEER_ORG}/{_PIONEER_REPO} era instances are registered; "
            f"check that {_PIONEER_ORG}/__init__.py imports every era module"
        )
    for low, high, era_cls in eras:
        if low < number <= high:
            return era_cls
    for low, high, era_cls in eras:
        if low <= number <= high:
            return era_cls
    raise ValueError(
        f"PR {number} falls outside every registered {_PIONEER_ORG}/"
        f"{_PIONEER_REPO} era; add the era that owns it rather than routing it "
        f"to the wrong toolchain"
    )


@Instance.register(_PIONEER_ORG, _PIONEER_REPO)
class Pioneer(Instance):
    def __new__(cls, pr: PullRequest, config: Config, *args, **kwargs):
        return _pioneer_era_for(pr.number)(pr, config, *args, **kwargs)
