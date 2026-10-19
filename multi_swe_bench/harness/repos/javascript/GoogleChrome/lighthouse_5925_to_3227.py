import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL_NAME = "lighthouse_5925_to_3227"
_NODE_IMAGE = "node:18"
_APT_PACKAGES = "ca-certificates build-essential pkg-config python3 git"
_INSTALL_TIMEOUT = "1000000"


def _render(template: str, **tokens: object) -> str:
    out = template
    for key, value in tokens.items():
        out = out.replace(f"@@{key.upper()}@@", str(value))
    leftover = re.findall(r"@@[A-Z_]+@@", out)
    if leftover:
        raise ValueError(f"unsubstituted template tokens: {sorted(set(leftover))}")
    return out


# Shared base for the whole shard: full-history clone, pinned to nothing. The
# `# syntax` line opts out of DockerfileEnhancer, which would otherwise rewrite
# the clone into `git checkout ${BASE_COMMIT}` + history scrub and pin this
# shared image to a single PR. Checkout and prune happen in the PR layer.
_BASE_DOCKERFILE = """# syntax=docker/dockerfile:1.6

FROM @@NODE_IMAGE@@

ARG TARGETARCH
ARG REPO_URL="https://github.com/@@ORG@@/@@REPO@@.git"
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
    http_proxy=${http_proxy} \\
    https_proxy=${https_proxy} \\
    HTTP_PROXY=${HTTP_PROXY} \\
    HTTPS_PROXY=${HTTPS_PROXY} \\
    no_proxy=${no_proxy} \\
    NO_PROXY=${NO_PROXY} \\
    SSL_CERT_FILE=${CA_CERT_PATH} \\
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\
    CURL_CA_BUNDLE=${CA_CERT_PATH} \\
    PUPPETEER_SKIP_DOWNLOAD=1

LABEL org.opencontainers.image.title="@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.description="@@ORG@@/@@REPO@@ Docker image" \\
      org.opencontainers.image.source="https://github.com/@@ORG@@/@@REPO@@" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

@@GLOBAL_ENV@@

RUN apt-get update && apt-get install -y --no-install-recommends @@APT_PACKAGES@@ && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

@@CODE@@

@@CLEAR_ENV@@

CMD ["/bin/bash"]
"""

_PR_DOCKERFILE = """FROM @@BASE_IMAGE@@

@@PREPARE_COPIES@@
WORKDIR /home/@@REPO@@

RUN bash /home/prepare.sh

@@GRADED_COPIES@@
RUN set -eux; \\
    git checkout --detach @@SHA@@; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse @@SHA@@)"; \\
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

_CHECK_GIT_CHANGES_SH = """#!/bin/bash
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

_PREPARE_SH = """#!/bin/bash
set -e

cd /home/@@REPO@@

git reset --hard
git clean -fdxq
bash /home/check_git_changes.sh

if ! git cat-file -e @@SHA@@ 2>/dev/null; then
  git remote add origin https://github.com/@@ORG@@/@@REPO@@.git 2>/dev/null || git remote set-url origin https://github.com/@@ORG@@/@@REPO@@.git
  git fetch --quiet origin @@SHA@@ || git fetch --quiet origin "+refs/heads/*:refs/mswb/heads/*" || true
fi
git checkout --detach @@SHA@@
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
git remote remove origin 2>/dev/null || true
bash /home/check_git_changes.sh

yarn install --frozen-lockfile --network-timeout @@INSTALL_TIMEOUT@@ || yarn install --network-timeout @@INSTALL_TIMEOUT@@ || true

# Hard gate: the install above tolerates failure, so assert here that the
# runner run.sh will pick exists and the core package actually loads.
if grep -qE '"unit-core": *"jest' package.json; then
  test -x node_modules/.bin/jest
else
  test -x node_modules/.bin/mocha
fi
node -e "require('./lighthouse-core'); console.log('DEPS_OK')"
"""

# Mocha 3.x loads every spec file in one process before running any test, so a
# single file whose require() fails (e.g. a test.patch spec importing a module
# only fix.patch adds) aborts the whole run with zero results. This runner
# loads each file under try/catch, skips the ones that cannot load yet, and
# runs everything else.
_MOCHA_RUNNER_JS = """'use strict';
const path = require('path');
const Mocha = require(require.resolve('mocha', {paths: [process.cwd()]}));

const mocha = new Mocha({reporter: 'spec', timeout: 60000});
process.argv.slice(2).forEach(file => mocha.addFile(file));

mocha.loadFiles = function(fn) {
  const self = this;
  const suite = this.suite;
  this.files.forEach(file => {
    file = path.resolve(file);
    suite.emit('pre-require', global, file, self);
    try {
      suite.emit('require', require(file), file, self);
    } catch (err) {
      console.error(`mocha_safe_run: skipped ${path.relative(process.cwd(), file)}: ${err.message.split('\\n')[0]}`);
    }
    suite.emit('post-require', global, file, self);
  });
  fn && fn();
};

mocha.run(failures => {
  process.exitCode = failures ? 1 : 0;
});
"""

_RUN_TESTS_SH = """#!/bin/bash
set -eo pipefail

export CI=true

cd /home/@@REPO@@

@@PATCH_STEP@@

if grep -qE '"unit-core": *"jest' package.json; then
  yarn jest --verbose --runInBand "lighthouse-core/|lighthouse-cli/"
else
  node /home/mocha_safe_run.js $(find lighthouse-core/test -name '*-test.js' 2>/dev/null | sort)
fi
"""

_APPLY_TEST_PATCH = "git apply --whitespace=nowarn /home/test.patch\n"
_APPLY_BOTH_PATCHES = "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"

_PASS_MARKS = "✔✓√"
_FAIL_MARKS = "×✕✗✘✖"
_SKIP_MARKS = "○◌"
_RESULT_RE = re.compile(
    r"^(?P<mark>[" + _PASS_MARKS + _FAIL_MARKS + _SKIP_MARKS + r"-])\s+(?P<name>\S.*)$"
)
_NUMBERED_FAIL_RE = re.compile(r"^\d+\)\s+(?P<name>\S.*)$")
_FILE_HEADER_RE = re.compile(r"^(?:PASS|FAIL)\s+(?P<file>\S+)")
_SUMMARY_RE = re.compile(
    r"^(?:\d+ (?:passing|failing|pending|todo)\b|(?:Tests?|Test Suites?|Snapshots?|Time):)"
)
_TRAILING_RE = re.compile(r"\s*\(\d+(?:\.\d+)?\s*(?:ms|s|m)\)$")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _normalize_lines(test_log: str) -> list[str]:
    text = _OSC_RE.sub("", test_log)
    text = _CSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _clean_name(name: str) -> str:
    return _TRAILING_RE.sub("", name).strip()


class LighthouseImageBase(Image):
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
        return _NODE_IMAGE

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL_NAME}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        if self.config.need_clone:
            code = (
                f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\\n'
                f"    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null"
            )
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return _render(
            _BASE_DOCKERFILE,
            node_image=self.dependency(),
            org=self.pr.org,
            repo=self.pr.repo,
            global_env=self.global_env,
            apt_packages=_APT_PACKAGES,
            code=code,
            clear_env=self.clear_env,
        )


class LighthouseImageDefault(Image):
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
        return LighthouseImageBase(self.pr, self._config)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES_SH),
            File(".", "mocha_safe_run.js", _MOCHA_RUNNER_JS),
            File(
                ".",
                "prepare.sh",
                _render(
                    _PREPARE_SH,
                    repo=repo,
                    org=self.pr.org,
                    sha=self.pr.base.sha,
                    install_timeout=_INSTALL_TIMEOUT,
                ),
            ),
            File(
                ".",
                "run.sh",
                _render(_RUN_TESTS_SH, repo=repo, patch_step=""),
            ),
            File(
                ".",
                "test-run.sh",
                _render(
                    _RUN_TESTS_SH,
                    repo=repo,
                    patch_step=_APPLY_TEST_PATCH,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                _render(
                    _RUN_TESTS_SH,
                    repo=repo,
                    patch_step=_APPLY_BOTH_PATCHES,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        prepare_names = {"check_git_changes.sh", "prepare.sh"}
        prepare_copies = "".join(
            f"COPY {file.name} /home/\n"
            for file in self.files()
            if file.name in prepare_names
        )
        graded_copies = "".join(
            f"COPY {file.name} /home/\n"
            for file in self.files()
            if file.name not in prepare_names
        )
        return _render(
            _PR_DOCKERFILE,
            base_image=base.image_full_name(),
            prepare_copies=prepare_copies,
            graded_copies=graded_copies,
            repo=self.pr.repo,
            sha=self.pr.base.sha,
        )


@Instance.register("GoogleChrome", _INTERVAL_NAME)
class LIGHTHOUSE_5925_TO_3227(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return LighthouseImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        current_file = ""
        suite_stack: list[tuple[int, str]] = []

        for raw_line in _normalize_lines(test_log):
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped:
                continue

            header = _FILE_HEADER_RE.match(stripped)
            if header:
                current_file = header.group("file")
                continue

            if _SUMMARY_RE.match(stripped):
                break

            indent = len(line) - len(line.lstrip())

            match = _RESULT_RE.match(stripped)
            numbered = None if match else _NUMBERED_FAIL_RE.match(stripped)
            if match or numbered:
                prefix = [title for level, title in suite_stack if level < indent]
                if current_file:
                    prefix = [current_file] + prefix
                name = _clean_name((match or numbered).group("name"))
                if not name or name.startswith("/"):
                    continue
                full_name = " > ".join(prefix + [name])
                if numbered:
                    failed_tests.add(full_name)
                elif match.group("mark") in _PASS_MARKS:
                    passed_tests.add(full_name)
                elif match.group("mark") in _FAIL_MARKS:
                    failed_tests.add(full_name)
                else:
                    skipped_tests.add(full_name)
                continue

            if indent < 2:
                continue

            while suite_stack and suite_stack[-1][0] >= indent:
                suite_stack.pop()
            suite_stack.append((indent, stripped))

        failed_tests -= passed_tests
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
