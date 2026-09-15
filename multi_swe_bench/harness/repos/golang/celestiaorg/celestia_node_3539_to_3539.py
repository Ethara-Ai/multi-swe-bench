import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

GO_TEST_CMD = "go test -v -count=1 -timeout 900s ./blob/..."

BASELINE_GEN = """go test -v -count=1 -timeout 900s ./blob/... 2>&1 | awk '
  /^(ok|FAIL|\\?)[ \\t]+/ { pkg=$2; for (i=1;i<=n;i++) print pkg "\\t" names[i]; n=0; next }
  /^[ \\t]*--- (PASS|FAIL|SKIP): / { names[++n]=$3 }
' > /home/baseline_tests.txt || true
"""

SALVAGE_INLINE = """if [ -f /home/stage.log ] && grep -q '\\[build failed\\]' /home/stage.log; then
  grep '\\[build failed\\]' /home/stage.log | awk '{ print $2 }' | sort -u | while IFS= read -r pkg; do
    [ -n "$pkg" ] || continue
    awk -F'\\t' -v p="$pkg" '$1 == p { print "--- FAIL: " $2 " (0.00s)" }' /home/baseline_tests.txt
    printf 'FAIL\\t%s\\t0.000s\\n' "$pkg"
  done
fi
"""


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
        return "golang:1.22-bookworm"

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return "base-3539-to-3539"

    def workdir(self) -> str:
        return "base-3539-to-3539"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org = self.pr.org
        repo = self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
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
    git \\
    gettext \\
    gnupg \\
    make \\
    sudo \\
    wget \\
    && rm -rf /var/lib/apt/lists/*

RUN go env -w GOFLAGS=-buildvcs=false


WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        test_cmd = GO_TEST_CMD

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
                """#!/bin/bash
set -e

cd /home/{pr.repo}
bash /home/check_git_changes.sh

go mod download all || true
go build ./... || true

{baseline_gen}
""".format(pr=self.pr, baseline_gen=BASELINE_GEN),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CGO_ENABLED=1
cd /home/{pr.repo}
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc

""".format(pr=self.pr, test_cmd=test_cmd, salvage=SALVAGE_INLINE),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CGO_ENABLED=1
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc

""".format(pr=self.pr, test_cmd=test_cmd, salvage=SALVAGE_INLINE),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CGO_ENABLED=1
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
set +e
{test_cmd} 2>&1 | tee /home/stage.log
rc=${{PIPESTATUS[0]}}
set -e
{salvage}exit $rc

""".format(pr=self.pr, test_cmd=test_cmd, salvage=SALVAGE_INLINE),
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

        global_env = f"\n{self.global_env}\n" if self.global_env else ""

        return f"""FROM {name}:{tag}
{global_env}
{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {self.pr.base.sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {self.pr.base.sha})"; \\
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

{prepare_commands}
{self.clear_env}

"""


@Instance.register("celestiaorg", "celestia_node_3539_to_3539")
class CELESTIA_NODE_3539_TO_3539(Instance):
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_pass = re.compile(r"^--- PASS:\s+(\S+)")
        re_fail = re.compile(r"^--- FAIL:\s+(\S+)")
        re_skip = re.compile(r"^--- SKIP:\s+(\S+)")
        re_build_fail = re.compile(r"^FAIL\s+(\S+)\s+\[build failed\]")

        for line in clean_log.splitlines():
            line = line.strip()

            match = re_pass.match(line)
            if match:
                passed_tests.add(match.group(1))
                continue

            match = re_fail.match(line)
            if match:
                failed_tests.add(match.group(1))
                continue

            match = re_skip.match(line)
            if match:
                skipped_tests.add(match.group(1))
                continue

            match = re_build_fail.match(line)
            if match:
                failed_tests.add(match.group(1))

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
