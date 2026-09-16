
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "oh-my-claudecode_2699_to_2461"
_BASE_TAG = "base-2699_to_2461"

_NODE_IMAGE = "node:20-bookworm"

_VITEST_REPORT = "/home/vitest-report.json"

_VITEST_FLAGS = "--no-file-parallelism --testTimeout=60000 --hookTimeout=60000"


_EMIT_TESTCASES_PY = r'''"""Turn a vitest JSON report into the TESTCASE lines parse_log consumes.

Only ``TESTCASE <STATUS> <identifier>`` lines are parsed by the harness; every
other line printed here is diagnostics for a human reading the stage log. The
identifier is ``<repo-relative file> > <describe...> > <test title>``, which is
the shape report.py's ``_test_name_matches_files`` expects for a JS/TS suite.
"""

import json
import os
import sys

_CONTROL = dict.fromkeys(range(32), " ")
_CONTROL[127] = " "


def _flatten(text):
    return " ".join(str(text).translate(_CONTROL).split())


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("usage: emit_testcases.py <vitest-report.json> <repo-root>\n")
        return 2

    report_path, root = sys.argv[1], sys.argv[2]
    try:
        with open(report_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        sys.stderr.write("EMIT_ERROR unreadable report %s: %s\n" % (report_path, exc))
        return 1

    prefixes = []
    for candidate in (root, os.path.realpath(root)):
        candidate = candidate.rstrip("/") + "/"
        if candidate not in prefixes:
            prefixes.append(candidate)

    suites = data.get("testResults") or []
    lines = []
    failures = []
    nocases = []

    for suite in suites:
        name = (suite.get("name") or "").replace("\\", "/")
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        while name.startswith("./"):
            name = name[2:]

        cases = suite.get("assertionResults") or []
        if not cases:
            nocases.append(
                "%s :: %s" % (name, _flatten(suite.get("message") or "")[:300])
            )
            continue

        for case in cases:
            title = _flatten(case.get("title") or "")
            if not title:
                continue
            parts = [name]
            for ancestor in case.get("ancestorTitles") or []:
                ancestor = _flatten(ancestor)
                if ancestor:
                    parts.append(ancestor)
            parts.append(title)
            identifier = " > ".join(parts)

            status = (case.get("status") or "").strip().lower()
            if status in ("failed", "error"):
                label = "FAILED"
                for message in (case.get("failureMessages") or [])[:1]:
                    failures.append(
                        "FAILURE %s :: %s" % (identifier, _flatten(message)[:300])
                    )
            elif status in ("skipped", "pending", "todo", "disabled"):
                label = "SKIPPED"
            else:
                label = "PASSED"
            lines.append("TESTCASE %s %s" % (label, identifier))

    for line in lines:
        sys.stdout.write(line + "\n")
    sys.stdout.write(
        "STAGE_SUMMARY suites=%d testcases=%d nocases=%d\n"
        % (len(suites), len(lines), len(nocases))
    )
    for entry in nocases:
        sys.stdout.write("NOCASES %s\n" % entry)
    for entry in failures:
        sys.stdout.write("%s\n" % entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


_CHECK_GIT_CHANGES_SH = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


_PREPARE_SH = """#!/bin/bash
set -e

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export npm_config_audit=false
export npm_config_fund=false
export npm_config_update_notifier=false

cat > /home/emit_testcases.py <<'EMIT_TESTCASES_PY_EOF'
__EMIT_TESTCASES_PY__
EMIT_TESTCASES_PY_EOF

cd /home/__REPO__

git reset --hard
git clean -fdx
bash /home/check_git_changes.sh

if ! git cat-file -e __BASE_SHA__^{commit} 2>/dev/null; then
    git remote add origin "https://github.com/__ORG__/__REPO__.git" 2>/dev/null || true
    git fetch --quiet --no-tags origin __BASE_SHA__
fi

git checkout --detach __BASE_SHA__
bash /home/check_git_changes.sh

npm ci --no-audit --no-fund || npm install --no-audit --no-fund || true

test -d node_modules
test -x node_modules/.bin/vitest
npx --no-install vitest --version
node -e "require('./package.json'); console.log('DEPS_OK')"
python3 -c "import json, sys; print('EMIT_TOOLCHAIN_OK')"
"""


_SCRIPT_HEADER = """#!/bin/bash
set -eo pipefail

export CI=true
export NODE_ENV=test
export NODE_OPTIONS="--max-old-space-size=4096"
export FORCE_COLOR=0
export NO_COLOR=1
export TZ=UTC

cd /home/__REPO__
"""


_APPLY_TEST_PATCH = """
git apply --whitespace=nowarn /home/test.patch"""


_APPLY_BOTH_PATCHES = """
git apply --whitespace=nowarn /home/test.patch /home/fix.patch"""


_EXEC_TESTS = """
rm -f __REPORT__

STATUS=0
npx --no-install vitest run \\
    --reporter=json \\
    --outputFile=__REPORT__ \\
    __VITEST_FLAGS__ || STATUS=$?

test -s __REPORT__

python3 /home/emit_testcases.py __REPORT__ /home/__REPO__

exit $STATUS
"""


_RUN_SH = _SCRIPT_HEADER + _EXEC_TESTS
_TEST_RUN_SH = _SCRIPT_HEADER + _APPLY_TEST_PATCH + _EXEC_TESTS
_FIX_RUN_SH = _SCRIPT_HEADER + _APPLY_BOTH_PATCHES + _EXEC_TESTS


_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH}

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \\
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \\
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV CI=true \\
    NODE_ENV=test \\
    NODE_OPTIONS=--max-old-space-size=4096 \\
    NPM_CONFIG_AUDIT=false \\
    NPM_CONFIG_FUND=false \\
    NPM_CONFIG_UPDATE_NOTIFIER=false \\
    FORCE_COLOR=0 \\
    NO_COLOR=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates curl python3 build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${REPO_URL}" /home/__REPO__

CMD ["/bin/bash"]
"""


_PRUNE = """RUN set -eux; \\
    git checkout --detach "__BASE_SHA__"; \\
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
    test "$(git rev-parse HEAD)" = "__BASE_SHA__"; \\
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


_ANSI_RE = re.compile(r"\x1B\[[0-9;?]*[a-zA-Z]")
_TESTCASE_RE = re.compile(r"^TESTCASE\s+(PASSED|FAILED|SKIPPED)\s+(\S.*?)\s*$")


def oh_my_claudecode_2699_parse_log(test_log: str) -> TestResult:
    clean_log = _ANSI_RE.sub("", test_log).replace("\r", "")

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for line in clean_log.split("\n"):
        match = _TESTCASE_RE.match(line)
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if status == "FAILED":
            failed_tests.add(name)
        elif status == "SKIPPED":
            skipped_tests.add(name)
        else:
            passed_tests.add(name)

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


class OhMyClaudecodeEraImageBase2699To2461(Image):

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
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()
        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", image_name)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
        )


class OhMyClaudecodeEraImageDefault2699To2461(Image):

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
        return OhMyClaudecodeEraImageBase2699To2461(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def _fill(self, text: str) -> str:
        return (
            text.replace("__EMIT_TESTCASES_PY__", _EMIT_TESTCASES_PY)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__BASE_SHA__", self.pr.base.sha)
            .replace("__REPORT__", _VITEST_REPORT)
            .replace("__VITEST_FLAGS__", _VITEST_FLAGS)
        )

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "prepare.sh", self._fill(_PREPARE_SH)),
            File(".", "run.sh", self._fill(_RUN_SH)),
            File(".", "test-run.sh", self._fill(_TEST_RUN_SH)),
            File(".", "fix-run.sh", self._fill(_FIX_RUN_SH)),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()
        if isinstance(dep, str):
            raise ValueError("ImageDefault dependency must be an Image")

        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

{copy_commands}
RUN bash /home/prepare.sh

{self._fill(_PRUNE)}"""


@Instance.register("Yeachan-Heo", _INTERVAL_NAME)
class OH_MY_CLAUDECODE_2699_TO_2461(Instance):

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OhMyClaudecodeEraImageDefault2699To2461(self.pr, self._config)

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
        return oh_my_claudecode_2699_parse_log(test_log)


Instance.register("Yeachan-Heo", "oh_my_claudecode_2699_to_2461")(
    OH_MY_CLAUDECODE_2699_TO_2461
)
