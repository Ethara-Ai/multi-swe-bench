from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NODE_IMAGE = "node:12"

_CA_SYMLINKS = """RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"""

_CHECK_GIT_CHANGES = """set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "check_git_changes: Uncommitted changes"
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_DETECT_PROJECTS = """set -u
PATCH_FILE="$1"
MODE="$2"
if [ ! -f "$PATCH_FILE" ]; then
    exit 0
fi
grep '^diff --git' "$PATCH_FILE" | sed 's|diff --git a/||;s| b/.*||' | while read -r fpath; do
    dir="$fpath"
    while [ "$dir" != "." ] && [ "$dir" != "/" ] && [ -n "$dir" ]; do
        if [ -f "/home/__REPO__/$dir/package.json" ]; then
            if [ "$MODE" = "dir" ]; then
                echo "$dir"
            else
                node -e "try{console.log(require('/home/__REPO__/'+process.argv[1]+'/package.json').name||'')}catch(e){}" "$dir"
            fi
            break
        fi
        dir=$(dirname "$dir")
    done
done | grep -v '^$' | sort -u
"""

_RUN_JEST = """set -u
PDIR="$1"
REPO="/home/__REPO__"

if [ ! -d "$REPO/$PDIR" ]; then
    exit 0
fi

if [ ! -f "$REPO/$PDIR/config/jest.json" ] && [ ! -f "$REPO/$PDIR/config/jest.config.json" ]; then
    exit 0
fi

cd "$REPO/$PDIR"

if [ ! -d "lib" ]; then
    echo "=== No lib/ directory in $PDIR, skipping jest ==="
    exit 0
fi

JEST_BIN=""
if [ -x "./node_modules/.bin/jest" ]; then
    JEST_BIN="./node_modules/.bin/jest"
elif [ -x "$REPO/common/temp/node_modules/.bin/jest" ]; then
    JEST_BIN="$REPO/common/temp/node_modules/.bin/jest"
else
    JEST_BIN=$(find "$REPO/common/temp" -not -path '*/rush-recycler/*' -path '*/jest-cli/bin/jest.js' -print -quit 2>/dev/null || true)
fi

if [ -z "$JEST_BIN" ]; then
    echo "=== No jest binary found for $PDIR ==="
    exit 0
fi

echo "=== Running jest in $PDIR ==="
"$JEST_BIN" --rootDir . --roots lib --testRegex '.*\\.test\\.js$' --no-coverage --no-cache --no-watchman --verbose 2>&1 || true
echo "=== Finished jest in $PDIR ==="
"""

_PREPARE = """set -euo pipefail

cd /home/__REPO__
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach __BASE_COMMIT__
bash /home/check_git_changes.sh

export CI=1
export npm_config_yes=true
export SKIP_PREFLIGHT_CHECK=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

node common/scripts/install-run-rush.js install --bypass-policy

PROJECT_DIRS=$(
    { bash /home/detect_project_dirs.sh /home/test.patch dir; \\
      bash /home/detect_project_dirs.sh /home/fix.patch dir; } | sort -u
)

test -n "$PROJECT_DIRS"

test -d /home/__REPO__/common/temp/node_modules

for pdir in $PROJECT_DIRS; do
    test -d "/home/__REPO__/$pdir/node_modules"
    test -x "/home/__REPO__/$pdir/node_modules/.bin/jest"
done
"""

_RUN_TEMPLATE = """set -u

cd /home/__REPO__
__APPLY__
export CI=1
export npm_config_yes=true
export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=1

node common/scripts/install-run-rush.js install --bypass-policy || true

PROJECTS=$(
    { bash /home/detect_project_dirs.sh /home/test.patch name; \\
      bash /home/detect_project_dirs.sh /home/fix.patch name; } | sort -u
)
PROJECT_DIRS=$(
    { bash /home/detect_project_dirs.sh /home/test.patch dir; \\
      bash /home/detect_project_dirs.sh /home/fix.patch dir; } | sort -u
)

for proj in $PROJECTS; do
    node common/scripts/install-run-rush.js build --to "$proj" || true
done

for pdir in $PROJECT_DIRS; do
    bash /home/run_jest.sh "$pdir"
done
"""


def _render(template: str, repo: str, base_commit: str = "") -> str:
    return template.replace("__REPO__", repo).replace("__BASE_COMMIT__", base_commit)


def _run_script(repo: str, apply_cmd: str) -> str:
    return _render(_RUN_TEMPLATE, repo).replace("__APPLY__", apply_cmd)


class RushstackImageBase(Image):
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
            fetch = f"""RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"""
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

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
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

{self.global_env}

{_CA_SYMLINKS}

RUN git config --global --add safe.directory '*'

WORKDIR /home/

{fetch}

{self.clear_env}

CMD ["/bin/bash"]
"""


class RushstackImageDefault(Image):
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
        return RushstackImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "detect_project_dirs.sh",
                _render(_DETECT_PROJECTS, self.pr.repo),
            ),
            File(".", "run_jest.sh", _render(_RUN_JEST, self.pr.repo)),
            File(
                ".",
                "prepare.sh",
                _render(_PREPARE, self.pr.repo, self.pr.base.sha),
            ),
            File(".", "run.sh", _run_script(self.pr.repo, "")),
            File(
                ".",
                "test-run.sh",
                _run_script(
                    self.pr.repo, "git apply --whitespace=nowarn /home/test.patch"
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _run_script(
                    self.pr.repo,
                    "git apply --whitespace=nowarn /home/test.patch /home/fix.patch",
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""# syntax=docker/dockerfile:1.6
FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{self.pr.repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-parse HEAD)" = "{sha}"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{self.pr.repo}/.gitmodules ]; then \\
        cd /home/{self.pr.repo} && git submodule foreach --recursive ' \\
            git remote remove origin 2>/dev/null || true; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

{self.clear_env}

"""


@Instance.register("microsoft", "rushstack")
class Rushstack(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return RushstackImageDefault(self.pr, self._config)

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

        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        re_suite_pass = re.compile(r"^\s*PASS\s+(.+?)\s*$")
        re_suite_fail = re.compile(r"^\s*FAIL\s+(.+?)\s*$")
        re_case_pass = re.compile(r"^\s*[\u2713\u2714\u221a]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_case_fail = re.compile(r"^\s*[\u2715\u2718\u00d7]\s+(.*?)(?:\s+\(\d+\s*m?s\))?\s*$")
        re_case_skip = re.compile(r"^\s*\u25cb\s+skipped\s+(.*?)\s*$")

        for line in test_log.splitlines():
            line = ansi_escape.sub("", line).rstrip()
            if not line.strip():
                continue

            match = re_suite_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_case_fail.match(line)
            if match:
                failed_tests.add(match.group(1).strip())
                continue

            match = re_case_skip.match(line)
            if match:
                skipped_tests.add(match.group(1).strip())
                continue

            match = re_suite_pass.match(line)
            if match:
                name = match.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

            match = re_case_pass.match(line)
            if match:
                name = match.group(1).strip()
                if name not in failed_tests:
                    passed_tests.add(name)
                continue

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
