import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Covers OPA PRs 3481, 3575 (Era B, go 1.16), 6153, 6272 (Era D, go 1.21),
# 6873 (Era E, go 1.24). Go version + test flags are selected per PR number.

_GO_IMAGE = {"b": "golang:1.16", "d": "golang:1.21", "e": "golang:1.24"}

# One shared base per Go era (3 bases), named after the PRs it serves so the
# base<->PR mapping is obvious: era b -> PRs 3481,3575 ; d -> 6153,6272 ; e -> 6873.
_ERA_LABEL = {"b": "3481-3575", "d": "6153-6272", "e": "6873"}


def _era(number: int) -> str:
    if number <= 3667:
        return "b"
    if number <= 6596:
        return "d"
    return "e"


def _test_cmd(number: int) -> str:
    mod = "" if _era(number) == "b" else "-mod=mod "
    return f"go test {mod}-v -count=1 ./..."


# awk filter: drop binary blobs and go.mod/go.sum/go.work hunks before applying
# a patch (matches the verified logic in the existing interval configs).
_FILTER_BINARY = r"""filter_binary() {
  awk '
    function flush() { if (section != "" && !isbin) printf "%s", section }
    /^diff --git / {
      flush(); section=""; isbin=0
      if ($0 ~ /\.(binpb|pb|png|ico|gz|tgz|wasm|pdf|jpg|jpeg|gif|zip|tar|woff|woff2|ttf|eot|bin)([ \t]|$)/) isbin=1
      if ($0 ~ /(go\.work\.sum|go\.work|go\.sum|go\.mod)$/) isbin=1
    }
    /^GIT binary patch$/ { isbin=1 }
    /^Binary files / { isbin=1 }
    { section = section $0 "\n" }
    END { flush() }
  ' "$1"
}"""


class OpaImageBase(Image):
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
        return _GO_IMAGE[_era(self.pr.number)]

    def image_tag(self) -> str:
        return f"base-pr-{_ERA_LABEL[_era(self.pr.number)]}"

    def workdir(self) -> str:
        return f"base-pr-{_ERA_LABEL[_era(self.pr.number)]}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

RUN git clone --filter=blob:none "${{REPO_URL}}" /home/{self.pr.repo}

CMD ["/bin/bash"]
"""


class OpaImageDefault(Image):
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
        return OpaImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        test_cmd = _test_cmd(self.pr.number)

        check_git_changes = """#!/bin/bash
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

        prepare = """#!/bin/bash
set -e

cd /home/{repo}
bash /home/check_git_changes.sh
go mod download || true
{test_cmd} || true
""".format(repo=repo, test_cmd=test_cmd)

        run = """#!/bin/bash
set -e

cd /home/{repo}
{test_cmd}
""".format(repo=repo, test_cmd=test_cmd)

        test_run = """#!/bin/bash
set -e

cd /home/{repo}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
git apply --whitespace=nowarn /home/test.filtered.patch
{test_cmd}
""".format(repo=repo, filter=_FILTER_BINARY, test_cmd=test_cmd)

        fix_run = """#!/bin/bash
set -e

cd /home/{repo}

{filter}

filter_binary /home/test.patch > /home/test.filtered.patch
filter_binary /home/fix.patch  > /home/fix.filtered.patch
git apply --whitespace=nowarn /home/test.filtered.patch
git apply --whitespace=nowarn /home/fix.filtered.patch
{test_cmd}
""".format(repo=repo, filter=_FILTER_BINARY, test_cmd=test_cmd)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", check_git_changes),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        repo = self.pr.repo
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

ARG BASE_COMMIT="{sha}"

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

RUN set -eux; \\
    export GIT_AUTHOR_NAME=ethara GIT_AUTHOR_EMAIL=ci@ethara.ai \\
           GIT_COMMITTER_NAME=ethara GIT_COMMITTER_EMAIL=ci@ethara.ai; \\
    git checkout --detach "${{BASE_COMMIT}}"; \\
    BASE_TREE="$(git rev-parse "${{BASE_COMMIT}}^{{tree}}")"; \\
    NEW="$(git commit-tree "$BASE_TREE" -m "base ${{BASE_COMMIT}}")"; \\
    git reset --hard "$NEW"; \\
    git checkout --detach "$NEW"; \\
    git remote remove origin 2>/dev/null || true; \\
    git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags refs/replace \\
        | xargs -r -n1 git update-ref -d; \\
    git config --unset extensions.partialClone 2>/dev/null || true; \\
    git config --unset remote.origin.promisor 2>/dev/null || true; \\
    git reflog expire --expire=now --all; \\
    git reflog expire --expire-unreachable=now --all; \\
    git gc --prune=now --aggressive; \\
    git repack -a -d -l --quiet; \\
    rm -f .git/objects/info/alternates; \\
    git config --local gc.auto 0; \\
    git config --local fetch.recurseSubmodules false; \\
    git config --local remote.pushDefault ""; \\
    test "$(git rev-parse HEAD^{{tree}})" = "$BASE_TREE"; \\
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


@Instance.register("open-policy-agent", "opa_6873_to_3481")
class OPA_6873_TO_3481(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpaImageDefault(self.pr, self._config)

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

        re_pass = re.compile(r"^--- PASS: (\S+)")
        re_fail = re.compile(r"^--- FAIL: (\S+)")
        re_skip = re.compile(r"^--- SKIP: (\S+)")

        def get_base_name(name: str) -> str:
            idx = name.rfind("/")
            return name[:idx] if idx != -1 else name

        for line in test_log.splitlines():
            line = line.strip()
            if not line:
                continue

            m = re_pass.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in failed_tests:
                    passed_tests.add(test_name)
                    skipped_tests.discard(test_name)
                continue

            m = re_fail.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                passed_tests.discard(test_name)
                skipped_tests.discard(test_name)
                failed_tests.add(test_name)
                continue

            m = re_skip.match(line)
            if m:
                test_name = get_base_name(m.group(1))
                if test_name not in passed_tests and test_name not in failed_tests:
                    skipped_tests.add(test_name)
                continue

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
