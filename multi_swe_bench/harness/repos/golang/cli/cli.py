import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_IMAGE = "golang:1.13.15"
_BASE_APT = "ca-certificates curl build-essential git gnupg make python3 sudo wget"

_PR_NUMBERS: set = set()


class CliImageBase(Image):
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
        return GO_IMAGE

    def image_tag(self) -> str:
        nums = _PR_NUMBERS or {self.pr.number}
        return f"base-{min(nums)}-{max(nums)}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list:
        return []

    def dockerfile(self) -> str:
        image = self.dependency()
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
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
    GO111MODULE=on \\
    GOSUMDB=off \\
    CGO_ENABLED=1 \\
    http_proxy=${{http_proxy}} \\
    https_proxy=${{https_proxy}} \\
    HTTP_PROXY=${{HTTP_PROXY}} \\
    HTTPS_PROXY=${{HTTPS_PROXY}} \\
    no_proxy=${{no_proxy}} \\
    NO_PROXY=${{NO_PROXY}} \\
    SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \\
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/

RUN set -eux; \\
    mkdir -p /etc/pki/tls/certs /etc/ssl /etc/pki/ca-trust/extracted/pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/certs/ca-bundle.crt; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/cert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/tls/cacert.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem; \\
    ln -sf ${{CA_CERT_PATH}} /etc/ssl/certs/ca-bundle.crt

RUN set -eux; \\
    sed -i -e 's|http://deb.debian.org/debian|http://archive.debian.org/debian|g' \\
           -e 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|g' \\
           -e 's|http://deb.debian.org/debian-security|http://archive.debian.org/debian-security|g' \\
           /etc/apt/sources.list; \\
    sed -i '/buster-updates/d' /etc/apt/sources.list; \\
    apt-get -o Acquire::Check-Valid-Until=false update; \\
    apt-get install -y --no-install-recommends {_BASE_APT}; \\
    rm -rf /var/lib/apt/lists/*

RUN git clone "${{REPO_URL}}" /home/{repo}
CMD ["/bin/bash"]
"""


class CliImageDefault(Image):
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
        return CliImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list:
        org = self.pr.org
        repo = self.pr.repo
        sha = self.pr.base.sha

        check_git_changes_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            "if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then\n"
            '  echo "check_git_changes: Not inside a git repository"\n'
            "  exit 1\n"
            "fi\n"
            "if [[ -n $(git status --porcelain) ]]; then\n"
            '  echo "check_git_changes: Uncommitted changes"\n'
            "  git status --porcelain\n"
            "  exit 1\n"
            "fi\n"
            'echo "check_git_changes: No uncommitted changes"\n'
            "exit 0\n"
        )

        prepare_sh = (
            "#!/bin/bash\n"
            "set -e\n"
            f'BASE_COMMIT="${{BASE_COMMIT:-{sha}}}"\n'
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "bash /home/check_git_changes.sh\n"
            'git checkout "${BASE_COMMIT}"\n'
            "bash /home/check_git_changes.sh\n"
            "go mod download || true\n"
            "git checkout -- .\n"
            "bash /home/check_git_changes.sh\n"
        )

        run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "go test -v -count=1 ./...\n"
        )
        test_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch\n"
            "go test -v -count=1 ./...\n"
        )
        fix_run_sh = (
            "#!/bin/bash\n"
            "set -eo pipefail\n"
            f"cd /home/{repo}\n"
            "git reset --hard\n"
            "git clean -qfd\n"
            "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
            "go test -v -count=1 ./...\n"
        )

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes_sh),
            File(".", "prepare.sh", prepare_sh),
            File(".", "run.sh", run_sh),
            File(".", "test-run.sh", test_run_sh),
            File(".", "fix-run.sh", fix_run_sh),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        name = base.image_name()
        tag = base.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    cd /home/{repo}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse "${{BASE_COMMIT}}")"; \\
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
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi
"""


@Instance.register("cli", "cli")
class Cli(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config
        _PR_NUMBERS.add(pr.number)

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return CliImageDefault(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        re_pass_tests = [re.compile(r"--- PASS: (\S+)")]
        re_fail_tests = [re.compile(r"--- FAIL: (\S+)")]
        re_skip_tests = [re.compile(r"--- SKIP: (\S+)")]

        for line in test_log.splitlines():
            line = line.strip()
            for r in re_pass_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    if name in failed_tests:
                        continue
                    skipped_tests.discard(name)
                    passed_tests.add(name)
            for r in re_fail_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    passed_tests.discard(name)
                    skipped_tests.discard(name)
                    failed_tests.add(name)
            for r in re_skip_tests:
                m = r.match(line)
                if m:
                    name = m.group(1)
                    if name in passed_tests or name in failed_tests:
                        continue
                    skipped_tests.add(name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
