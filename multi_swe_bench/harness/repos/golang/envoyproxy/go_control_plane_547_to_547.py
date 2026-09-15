import re
from typing import Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

PRS = [547]
BASE_TAG = "base-547_to_547"
GO_IMAGE = "golang:1.16"

CHECK_GIT_CHANGES = r"""#!/bin/bash
set -euo pipefail

cd "$1"

if [ -n "$(git status --porcelain)" ]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

PREPARE_BODY = r"""set -euo pipefail

export CI=true
export GOPATH=/go
export GOFLAGS=-mod=mod

cd "$REPO_DIR"

bash /home/check_git_changes.sh "$REPO_DIR"

go version
go env GOCACHE

go mod download

go build ./pkg/... || true
go vet ./pkg/... > /dev/null 2>&1 || true

test -n "$(go list ./pkg/... 2>/dev/null)"

bash /home/check_git_changes.sh "$REPO_DIR"
"""

TEST_BODY = r"""export CI=true

cd "$REPO_DIR"

PKGS=$(go list ./pkg/... 2>/dev/null)

if [ -z "$PKGS" ]; then
    echo "GCP RUNNER: go list ./pkg/... produced nothing"
    exit 0
fi

echo '##### TESTMAP-BEGIN'
for p in $PKGS; do
    d=$(go list -f '{{.Dir}}' "$p" 2>/dev/null) || continue
    rel="${d#$REPO_DIR/}"
    find "$d" -maxdepth 1 -name '*_test.go' 2>/dev/null | while IFS= read -r f; do
        grep -oE '^func (Test[A-Za-z0-9_]+)\(' "$f" 2>/dev/null \
            | sed 's/^func //; s/($//; s/(//' \
            | while IFS= read -r t; do
                echo "${rel}/$(basename "$f")|$t"
            done
    done
done
echo '##### TESTMAP-END'

for p in $PKGS; do
    d=$(go list -f '{{.Dir}}' "$p" 2>/dev/null) || continue
    rel="${d#$REPO_DIR/}"
    echo "##### PKG: $rel"
    go test -race -v -timeout 30s -count=1 -parallel 100 "$p" 2>&1 || true
done
"""


class GoControlPlaneImageBase(Image):
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
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

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

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""


class GoControlPlaneImageDefault(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, Image]:
        return GoControlPlaneImageBase(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo_dir = f"/home/{self.pr.repo}"

        prepare = "#!/bin/bash\n"
        prepare += f'REPO_DIR="{repo_dir}"\n'
        prepare += "\n"
        prepare += PREPARE_BODY

        run = "#!/bin/bash\n"
        run += "set -uo pipefail\n"
        run += f'REPO_DIR="{repo_dir}"\n'
        run += "\n"
        run += TEST_BODY

        test_run = "#!/bin/bash\n"
        test_run += "set -uo pipefail\n"
        test_run += f'REPO_DIR="{repo_dir}"\n'
        test_run += "\n"
        test_run += f'cd "{repo_dir}"\n'
        test_run += "git apply --whitespace=nowarn /home/test.patch\n"
        test_run += "\n"
        test_run += TEST_BODY

        fix_run = "#!/bin/bash\n"
        fix_run += "set -uo pipefail\n"
        fix_run += f'REPO_DIR="{repo_dir}"\n'
        fix_run += "\n"
        fix_run += f'cd "{repo_dir}"\n'
        fix_run += "git apply --whitespace=nowarn /home/test.patch /home/fix.patch\n"
        fix_run += "\n"
        fix_run += TEST_BODY

        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "run.sh", run),
            File(".", "test-run.sh", test_run),
            File(".", "fix-run.sh", fix_run),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN git cat-file -e {sha}^{{commit}} 2>/dev/null \\
    || git fetch --no-tags --depth=2147483647 origin {sha} \\
    || git fetch --no-tags origin "+refs/pull/{self.pr.number}/head:refs/remotes/origin/pr-{self.pr.number}"

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
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

RUN bash /home/prepare.sh

{self.clear_env}
"""


class GoControlPlaneInstance(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GoControlPlaneImageDefault(self.pr, self._config)

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

        ansi = re.compile(r"\x1B\[[0-?9;]*[mK]")
        clean = ansi.sub("", test_log)

        testmap: dict[tuple[str, str], str] = {}
        in_map = False
        for line in clean.splitlines():
            s = line.strip()
            if s == "##### TESTMAP-BEGIN":
                in_map = True
                continue
            if s == "##### TESTMAP-END":
                in_map = False
                continue
            if in_map and "|" in s:
                path, _, name = s.partition("|")
                path, name = path.strip(), name.strip()
                if path and name:
                    d = path.rsplit("/", 1)[0] if "/" in path else "."
                    testmap[(d, name)] = path

        pkg_re = re.compile(r"^##### PKG:\s*(\S+)\s*$")
        res_re = re.compile(r"^\s*--- (PASS|FAIL|SKIP):\s+(\S+)")

        cur_dir = ""
        for line in clean.splitlines():
            m = pkg_re.match(line)
            if m:
                cur_dir = m.group(1)
                if cur_dir.startswith("./"):
                    cur_dir = cur_dir[2:]
                if cur_dir in ("...", ""):
                    cur_dir = "."
                continue

            m = res_re.match(line)
            if not m:
                continue
            status, name = m.group(1), m.group(2)

            parent = name.split("/", 1)[0]
            src = testmap.get((cur_dir, parent))
            if src is None:
                src = cur_dir or "."
            ident = f"{src}::{name}"

            if status == "PASS":
                passed_tests.add(ident)
            elif status == "FAIL":
                failed_tests.add(ident)
            else:
                skipped_tests.add(ident)

        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


@Instance.register("envoyproxy", "go_control_plane_547_to_547")
class GO_CONTROL_PLANE_547_TO_547(GoControlPlaneInstance):
    pass
