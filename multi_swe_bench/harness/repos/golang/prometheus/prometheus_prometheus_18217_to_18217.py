import json
import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_NUMBER_INTERVAL = "prometheus_prometheus_18217_to_18217"
_BASE_IMAGE = "golang:1.25-trixie"
_TEST_COMMAND = "go test -json -count=1 -timeout=60m ./..."

_GO_ENV = """export GOTOOLCHAIN=local
export CGO_ENABLED=1
export GOFLAGS=-buildvcs=false"""

_CHECK_GIT_CHANGES = """#!/bin/bash
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

_ISOLATE_UNCOMPILABLE = r"""#!/bin/bash
set -eo pipefail

work=/tmp/uncompilable
report=/tmp/uncompilable.log
mkdir -p "$work"
: > "$report"
: > "$work/list"

git_identity=(-c user.name=isolate -c user.email=isolate@localhost)

changed=$(git status --porcelain --no-renames --untracked-files=all | cut -c4- | awk '/\.go$/')

compiles_at_base() {
    local result=0
    local stashed=0
    if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
        git "${git_identity[@]}" stash push --include-untracked --quiet
        stashed=1
    fi
    go test -c -o /dev/null "$1" > /dev/null 2>&1 || result=1
    if [ "$stashed" = 1 ]; then
        git "${git_identity[@]}" stash pop --quiet
    fi
    return "$result"
}

flagged_names() {
    awk 'match($0, /[^ \t:\/]+\.go:[0-9]+/) { name = substr($0, RSTART, RLENGTH); sub(/:[0-9]+$/, "", name); print name }' "$work/output" | sort -u
}

for dir in $(printf '%s\n' "$changed" | xargs -r -n1 dirname | sort -u); do
    [ -d "$dir" ] || continue
    if go test -c -o /dev/null "./$dir" > "$work/output" 2>&1; then
        continue
    fi
    if ! compiles_at_base "./$dir"; then
        continue
    fi
    for pass in 1 2 3 4 5 6 7 8; do
        moved=0
        for name in $(flagged_names); do
            file="$name"
            if [ "$dir" != "." ]; then
                file="$dir/$name"
            fi
            [ -f "$file" ] || continue
            case "$name" in
                *_test.go) ;;
                *) grep -qxF "$file" <<< "$changed" || continue ;;
            esac
            saved="$work/$(printf '%s' "$file" | tr '/' '_')"
            if [ -e "$saved" ]; then
                rm -f "$file"
            else
                cp "$file" "$saved"
                printf '%s\t%s\n' "$file" "$saved" >> "$work/list"
                if git cat-file -e "HEAD:./$file" 2> /dev/null; then
                    git show "HEAD:./$file" > "$file"
                else
                    rm -f "$file"
                fi
            fi
            moved=1
        done
        if [ "$moved" = 0 ]; then
            break
        fi
        if go test -c -o /dev/null "./$dir" > "$work/output" 2>&1; then
            break
        fi
    done
done

while IFS=$'\t' read -r file saved; do
    package=$(go list -e -f '{{.ImportPath}}' "./$(dirname "$file")")
    sed -nE 's/^func (Test[A-Za-z0-9_]*)\([A-Za-z_][A-Za-z0-9_]* \*testing\.T\).*/\1/p' "$saved" | while IFS= read -r test; do
        printf '{"Action":"fail","Package":"%s","Test":"%s"}\n' "$package" "$test" >> "$report"
    done
done < "$work/list"
"""


class PrometheusImageBase18217To18217(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return f"base-{_NUMBER_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_NUMBER_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        org = self.pr.org
        repo = self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6

FROM {self.dependency()}

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
    git ca-certificates build-essential \\
    && rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{repo} && \\
    cd /home/{repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class PrometheusImageDefault18217To18217(Image):
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
        return PrometheusImageBase18217To18217(self.pr, self.config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        sha = self.pr.base.sha
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "isolate-uncompilable.sh", _ISOLATE_UNCOMPILABLE),
            File(
                ".",
                "prepare.sh",
                f"""#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach {sha}
bash /home/check_git_changes.sh

{_GO_ENV}
export GODEBUG=http2client=0

go mod download || go mod download || go mod download || true
go test -count=1 -exec /bin/true ./... || true

go build ./...
go list -deps -test ./... > /dev/null
echo DEPS_OK
""",
            ),
            File(
                ".",
                "run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
{_GO_ENV}

cd /home/{repo}
{_TEST_COMMAND}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
{_GO_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/isolate-uncompilable.sh
status=0
{_TEST_COMMAND} || status=$?
cat /tmp/uncompilable.log
exit "$status"
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""#!/bin/bash
set -eo pipefail

export CI=true
{_GO_ENV}

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{_TEST_COMMAND}
""",
            ),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        repo = self.pr.repo
        sha = self.pr.base.sha
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
        return f"""FROM {base.image_name()}:{base.image_tag()}

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
"""


@Instance.register("prometheus", _NUMBER_INTERVAL)
class PROMETHEUS_18217_TO_18217(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return PrometheusImageDefault18217To18217(self.pr, self._config)

    def run(self, run_cmd: str = "") -> str:
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        started_tests: set[str] = set()
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        outcomes = {"pass": passed_tests, "fail": failed_tests, "skip": skipped_tests}

        for line in clean_log.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            package = event.get("Package")
            test = event.get("Test")
            if not package or not test:
                continue
            name = f"{package}::{test}"
            action = event.get("Action")
            if action == "run":
                started_tests.add(name)
            elif action in outcomes:
                outcomes[action].add(name)

        failed_tests |= started_tests - passed_tests - skipped_tests
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
