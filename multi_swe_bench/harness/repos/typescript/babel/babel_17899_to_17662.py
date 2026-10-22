import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

NODE_IMAGE = "node:22-bookworm-slim"
BASE_IMAGE_TAG = "base-17662_to_17899"
WORKSPACE_ROOTS = ("packages", "codemods", "eslint")
JEST_REPORT_PATH = "/home/jest-report.json"
JEST_LIST_PATH = "/home/jest-listtests.txt"
JEST_BEGIN_MARKER = "-----BEGIN_JEST_JSON-----"
JEST_END_MARKER = "-----END_JEST_JSON-----"
TSTYCHE_BEGIN_MARKER = "-----BEGIN_TSTYCHE-----"
TSTYCHE_END_MARKER = "-----END_TSTYCHE-----"
TYPE_TEST_SUFFIX = ".tst.ts"
SUITE_LOAD_FAILURE = "<suite failed to load>"
NAME_SEPARATOR = " > "

PATCH_TARGET_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
TSTYCHE_FILE_RE = re.compile(r"^(?:pass|fail)\s+(\S+\.tst\.ts)\s*$")
TSTYCHE_TEST_RE = re.compile(r"^(\s+)(\+|×|✕|- skip|- fixme|- todo)\s+(.+?)\s*$")
TSTYCHE_GROUP_RE = re.compile(r"^(\s+)(\S.*?)\s*$")


def patch_targets(patch: str) -> list[str]:
    return PATCH_TARGET_RE.findall(patch or "")


def all_patch_targets(pr: PullRequest) -> list[str]:
    return patch_targets(pr.test_patch) + patch_targets(pr.fix_patch)


def touched_workspaces(pr: PullRequest) -> list[str]:
    workspaces = set()
    for path in all_patch_targets(pr):
        parts = path.split("/")
        if len(parts) > 2 and parts[0] in WORKSPACE_ROOTS:
            workspaces.add(f"{parts[0]}/{parts[1]}")
    return sorted(workspaces)


def jest_test_targets(pr: PullRequest) -> str:
    return " ".join(f"{workspace}/test" for workspace in touched_workspaces(pr))


def has_type_tests(pr: PullRequest) -> bool:
    return any(path.endswith(TYPE_TEST_SUFFIX) for path in all_patch_targets(pr))


def repo_root(pr: PullRequest) -> str:
    return f"/home/{pr.repo}"


def relative_path(path: str, root: str) -> str:
    cleaned = path.strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    prefix = f"{root}/"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    return cleaned


def fenced_section(log: str, begin: str, end: str) -> str:
    start = log.find(begin)
    if start < 0:
        return ""
    stop = log.find(end, start + len(begin))
    if stop < 0:
        return ""
    return log[start + len(begin) : stop]


def assertion_full_name(assertion: dict) -> str:
    ancestors = [
        str(part).strip()
        for part in (assertion.get("ancestorTitles") or [])
        if str(part).strip()
    ]
    title = str(assertion.get("title") or "").strip()
    if not title:
        title = str(assertion.get("fullName") or "").strip()
    if not title:
        return ""
    return NAME_SEPARATOR.join(ancestors + [title])


def collect_jest_results(
    log: str,
    root: str,
    passed: set[str],
    failed: set[str],
    skipped: set[str],
) -> None:
    payload = fenced_section(log, JEST_BEGIN_MARKER, JEST_END_MARKER).strip()
    if not payload:
        return
    try:
        report = json.loads(payload)
    except ValueError:
        return

    for suite in report.get("testResults") or []:
        path = relative_path(suite.get("name") or "", root)
        if not path:
            continue
        assertions = suite.get("assertionResults") or []
        occurrences: dict[str, int] = {}
        for assertion in assertions:
            title = assertion_full_name(assertion)
            if not title:
                continue
            occurrences[title] = occurrences.get(title, 0) + 1
            name = f"{path}::{title}"
            if occurrences[title] > 1:
                name = f"{name}#{occurrences[title]}"
            status = assertion.get("status")
            if status == "passed":
                passed.add(name)
            elif status == "failed":
                failed.add(name)
            else:
                skipped.add(name)
        if not assertions and suite.get("status") == "failed":
            failed.add(f"{path}::{SUITE_LOAD_FAILURE}")


def collect_tstyche_results(
    log: str,
    root: str,
    passed: set[str],
    failed: set[str],
    skipped: set[str],
) -> None:
    payload = fenced_section(log, TSTYCHE_BEGIN_MARKER, TSTYCHE_END_MARKER)
    if not payload:
        return

    path = ""
    groups: list[tuple[int, str]] = []
    for raw_line in payload.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        file_match = TSTYCHE_FILE_RE.match(line)
        if file_match:
            path = relative_path(file_match.group(1), root)
            groups = []
            continue

        if not path:
            continue

        test_match = TSTYCHE_TEST_RE.match(line)
        if test_match:
            indent = len(test_match.group(1))
            while groups and groups[-1][0] >= indent:
                groups.pop()
            title = NAME_SEPARATOR.join(
                [name for _, name in groups] + [test_match.group(3)]
            )
            entry = f"{path}::{title}"
            marker = test_match.group(2)
            if marker == "+":
                passed.add(entry)
            elif marker in ("×", "✕"):
                failed.add(entry)
            else:
                skipped.add(entry)
            continue

        group_match = TSTYCHE_GROUP_RE.match(line)
        if group_match:
            indent = len(group_match.group(1))
            while groups and groups[-1][0] >= indent:
                groups.pop()
            groups.append((indent, group_match.group(2)))


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
        return NODE_IMAGE

    def image_tag(self) -> str:
        return BASE_IMAGE_TAG

    def workdir(self) -> str:
        return BASE_IMAGE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

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

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates \\
    curl \\
    build-essential \\
    g++ \\
    git \\
    gnupg \\
    make \\
    python3 \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN corepack enable

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

WORKDIR /home/{self.pr.repo}

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

    def dependency(self) -> Optional[Image]:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        root = repo_root(self.pr)
        targets = jest_test_targets(self.pr)
        type_tests = has_type_tests(self.pr)

        tstyche_stage = (
            f"""
echo "{TSTYCHE_BEGIN_MARKER}"
timeout 1800 yarn tstyche --verbose 2>&1
echo "{TSTYCHE_END_MARKER}"
"""
            if type_tests
            else ""
        )

        tstyche_gate = "require.resolve('tstyche'); " if type_tests else ""

        refresh_workspace = """YARN_ENABLE_IMMUTABLE_INSTALLS=false YARN_ENABLE_CONSTRAINTS_CHECKS=false yarn install
make build
"""

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

cd {root}

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {self.pr.base.sha}
bash /home/check_git_changes.sh
test "$(git rev-parse HEAD)" = "{self.pr.base.sha}"

corepack enable
export YARN_ENABLE_IMMUTABLE_INSTALLS=false
export YARN_ENABLE_CONSTRAINTS_CHECKS=false
yarn install
test -f "$(yarn bin jest)"
make build

yarn node "$(yarn bin jest)" --ci --listTests --passWithNoTests \\
    --rootDir {root} {targets} > {JEST_LIST_PATH}
test -s {JEST_LIST_PATH}
node -e "require.resolve('jest'); require.resolve('jest-light-runner'); {tstyche_gate}console.log('DEPS_OK')"

""",
            ),
            File(
                ".",
                "run-tests.sh",
                f"""#!/bin/bash
set -uo pipefail

cd {root}

export CI=true
export BABEL_ENV=test
export NO_COLOR=1
export FORCE_COLOR=0
export NODE_OPTIONS="--max-old-space-size=4096"

rm -f {JEST_REPORT_PATH}

timeout 5400 yarn node "$(yarn bin jest)" \\
    --ci \\
    --json \\
    --outputFile={JEST_REPORT_PATH} \\
    --rootDir {root} \\
    --runInBand \\
    --forceExit \\
    --passWithNoTests \\
    {targets} 2>&1

echo "{JEST_BEGIN_MARKER}"
cat {JEST_REPORT_PATH} 2>/dev/null
echo
echo "{JEST_END_MARKER}"
{tstyche_stage}
test -s {JEST_REPORT_PATH}

""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {root}
export CI=true

bash /home/run-tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {root}
export CI=true

git apply --whitespace=nowarn /home/test.patch
{refresh_workspace}
bash /home/run-tests.sh

""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

cd {root}
export CI=true

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{refresh_workspace}
bash /home/run-tests.sh

""",
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{copy_commands}
ARG BASE_COMMIT="{self.pr.base.sha}"

RUN bash /home/prepare.sh

RUN set -eux; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse ${{BASE_COMMIT}})"; \\
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


@Instance.register("babel", "babel_17899_to_17662")
class BABEL_17899_TO_17662(Instance):
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
        clean_log = ANSI_RE.sub("", test_log)
        root = repo_root(self.pr)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        collect_jest_results(clean_log, root, passed_tests, failed_tests, skipped_tests)
        collect_tstyche_results(
            clean_log, root, passed_tests, failed_tests, skipped_tests
        )

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
