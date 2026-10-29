import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_PR_INTERVAL = "30205_to_40000"
_TEST_USER = "giteatest"
_BASE_TAG = f"base-{_PR_INTERVAL}"
_GO_IMAGE = "golang:1.27"
_TEST_TIMEOUT = "40m"
_INTEGRATION_DIR = "tests/integration"
_EXCLUDED_DIR_PREFIXES = ("vendor/", "modelmigration/", "tests/", "build/", "tools/")

_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)


def _patch_paths(*patches: str) -> list[str]:
    paths: set[str] = set()
    for patch in patches:
        for old, new in _DIFF_HEADER.findall(patch):
            paths.add(old)
            paths.add(new)
    return sorted(p for p in paths if p != "/dev/null")


def _unit_packages(*patches: str) -> list[str]:
    dirs: set[str] = set()
    for path in _patch_paths(*patches):
        if not path.endswith(".go"):
            continue
        if "/testdata/" in f"/{path}":
            continue
        if path.startswith(_EXCLUDED_DIR_PREFIXES):
            continue
        dirs.add(path.rsplit("/", 1)[0] if "/" in path else ".")
    return sorted(dirs)


def _integration_test_files(*patches: str) -> list[str]:
    files: set[str] = set()
    for path in _patch_paths(*patches):
        if not path.endswith("_test.go"):
            continue
        if not path.startswith(f"{_INTEGRATION_DIR}/"):
            continue
        if path.startswith(f"{_INTEGRATION_DIR}/migration-test/"):
            continue
        files.add(path)
    return sorted(files)


def _test_command(test_patch: str, fix_patch: str) -> str:
    packages = " ".join(f"'{d}'" for d in _unit_packages(test_patch, fix_patch))
    integration = " ".join(
        f"'{f}'" for f in _integration_test_files(test_patch, fix_patch)
    )

    lines = ['PKGS=""']
    if packages:
        lines += [
            f"for d in {packages}; do",
            '  if [ -d "$d" ] && ls "$d"/*.go >/dev/null 2>&1; then',
            '    PKGS="$PKGS ./$d"',
            "  fi",
            "done",
        ]

    lines.append('INTEG_RUN=""')
    if integration:
        lines += [
            'INTEG_NAMES=""',
            f"for f in {integration}; do",
            '  if [ -f "$f" ]; then',
            "    INTEG_NAMES=\"$INTEG_NAMES $(sed -n 's/^func \\(Test[A-Za-z0-9_]*\\)(.*/\\1/p' \"$f\")\"",
            "  fi",
            "done",
            'if [ -n "$(printf \'%s\' $INTEG_NAMES)" ]; then',
            "  INTEG_SEED=$(awk 'FNR==1{n=\"\"}"
            " /^func Test/{n=$0; sub(/^func /,\"\",n); sub(/\\(.*/,\"\",n)}"
            " /tests\\.PrepareTestEnv/{if(n!=\"\"){print n; exit}}'"
            f" $(ls {_INTEGRATION_DIR}/*_test.go 2>/dev/null | sort))",
            '  INTEG_NAMES="$INTEG_NAMES $INTEG_SEED"',
            "fi",
            "INTEG_RUN=$(printf '%s\\n' $INTEG_NAMES | sort -u | paste -sd'|' -)",
        ]

    lines += [
        "status=0",
        'if [ -n "$PKGS" ]; then',
        f"  go test -json -count=1 -timeout {_TEST_TIMEOUT} $PKGS || status=$?",
        "fi",
        'if [ -n "$INTEG_RUN" ]; then',
        "  go build -o gitea .",
        f'  go test -json -count=1 -timeout {_TEST_TIMEOUT} -run "^($INTEG_RUN)$" ./{_INTEGRATION_DIR} || status=$?',
        "fi",
        'exit "$status"',
    ]
    return "\n".join(lines)


class GiteaImageBase_30205_TO_40000(Image):
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
        return _GO_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

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

        return f"""# syntax=docker/dockerfile:1.6

FROM {_GO_IMAGE}

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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}} \\
    GIT_SSL_CAINFO=${{CA_CERT_PATH}}

ENV CGO_ENABLED=0 \\
    GOTOOLCHAIN=local \\
    GOPROXY=https://proxy.golang.org,direct \\
    GOPATH=/go \\
    GITEA_TEST_DATABASE=sqlite \\
    CI=true

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
    git ca-certificates gnupg openssh-client \\
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && \\
    if ! apt-get install -y --no-install-recommends git-lfs; then \\
        dpkg --configure -a || true; \\
    fi && \\
    test -x /usr/bin/git-lfs && \\
    rm -rf /var/lib/apt/lists/*

RUN id -u {_TEST_USER} >/dev/null 2>&1 || useradd -m -s /bin/bash {_TEST_USER}

RUN git config --system --add safe.directory '*' && \\
    git config --system user.name "Gitea Test" && \\
    git config --system user.email "gitea-test@example.com" && \\
    git config --system filter.lfs.clean 'git-lfs clean -- %f' && \\
    git config --system filter.lfs.smudge 'git-lfs smudge -- %f' && \\
    git config --system filter.lfs.process 'git-lfs filter-process' && \\
    git config --system filter.lfs.required true

WORKDIR /home/

{code}

RUN cd /home/{self.pr.repo} && go mod download

RUN chown -R {_TEST_USER}:{_TEST_USER} /go

CMD ["/bin/bash"]
"""


class GiteaImageDefault_30205_TO_40000(Image):
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
        return GiteaImageBase_30205_TO_40000(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_command = _test_command(self.pr.test_patch, self.pr.fix_patch)
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
                """#!/bin/bash
set -euo pipefail

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git cat-file -e {pr.base.sha}^{{commit}} 2>/dev/null || git fetch --no-tags origin {pr.base.sha}
git checkout --detach {pr.base.sha}
bash /home/check_git_changes.sh

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

go version
go mod download
go build -o gitea .

go build ./...
go test -c -o /dev/null ./{integration_dir}
echo DEPS_OK
""".format(pr=self.pr, integration_dir=_INTEGRATION_DIR),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{pr.repo}
{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
export GITEA_TEST_DATABASE=sqlite
export CGO_ENABLED=0
export GOTOOLCHAIN=local

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_command}
""".format(pr=self.pr, test_command=test_command),
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo
        sha = self.pr.base.sha

        return f"""FROM {base.image_full_name()}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
    git checkout --detach "{sha}"; \\
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
    test -z "$(git reflog)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f /home/{repo}/.gitmodules ]; then \\
        cd /home/{repo} && git submodule foreach --recursive ' \\
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

RUN chown -R {_TEST_USER}:{_TEST_USER} /home/{repo}

USER {_TEST_USER}
"""


@Instance.register("go-gitea", "gitea")
class GITEA_30205_TO_40000(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return GiteaImageDefault_30205_TO_40000(self.pr, self._config)

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
        log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        packages_with_tests: set[str] = set()
        broken_packages: set[str] = set()

        for line in log.splitlines():
            line = line.strip()
            if not line.startswith("{") or '"Action"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue

            action = event.get("Action")
            package = event.get("Package") or event.get("ImportPath") or ""
            package = package.split(" [", 1)[0].strip()
            test = event.get("Test") or ""
            if not package:
                continue

            if test:
                if action not in ("pass", "fail", "skip"):
                    continue
                packages_with_tests.add(package)
                name = f"{package}::{test}"
                if action == "pass":
                    passed_tests.add(name)
                elif action == "fail":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
            elif action in ("fail", "build-fail"):
                broken_packages.add(package)

        for package in broken_packages - packages_with_tests:
            failed_tests.add(f"{package}::[package]")

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
