import json
import posixpath
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "delve_4448_to_4181"
_GO_IMAGE = "golang:1.26.10-bookworm"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_HUNK_HEADER = re.compile(r"^@@ [^@]* @@ ?(.*)$")
_TEST_FUNC = re.compile(r"^func ((?:Test|Fuzz|Example)\w*)\(")
_VOLATILE_TIME = re.compile(r"time\.Date\([^()]*\)")
_VOLATILE_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")
_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch || "
    "git apply --whitespace=nowarn -C1 /home/fix.patch"
)

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

_PREPARE = """#!/bin/bash
set -e

cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

go mod download || true
go build ./...
go test -count=1 -exec /bin/true ./... > /dev/null
echo DEPS_OK
"""

_GUARD = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
targets=/home/test_targets.txt
[ -s "$targets" ] || exit 0

cut -f1 "$targets" | sort -u | while read -r dir; do
    if go test -count=1 -exec /bin/true "./$dir" > /dev/null 2>&1; then
        continue
    fi
    pkg=$(go list -e -f '{{.ImportPath}}' "./$dir")
    awk -F'\\t' -v d="$dir" '$1 == d {print $2 "\\t" $3}' "$targets" | sort -u | while IFS=$'\\t' read -r file name; do
        if [ -f "$file" ] && grep -q "^func ${name}(" "$file"; then
            printf '{"Action":"fail","Package":"%s","Test":"%s"}\\n' "$pkg" "$name"
        fi
    done
    git status --porcelain --untracked-files=all -- "$dir" | cut -c4- | while read -r file; do
        case "$file" in
            *_test.go) ;;
            *) continue ;;
        esac
        [ "$(dirname "$file")" = "$dir" ] || continue
        if git cat-file -e "HEAD:$file" 2>/dev/null; then
            git checkout -q HEAD -- "$file"
        else
            rm -f "$file"
        fi
    done
done
"""

_RESUME = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
log="$1"
rc=0
previous=""
for attempt in 1 2 3 4 5 6 7 8; do
    pending=$(awk '
        match($0, /"Action":"[a-z]+","Package":"[^"]*","Test":"[^"]*"/) {
            split(substr($0, RSTART, RLENGTH), f, "\\"")
            if (f[12] ~ /\\//) next
            key = f[8] "\\t" f[12]
            if (f[4] == "run") started[key] = 1
            else if (f[4] == "pass" || f[4] == "fail" || f[4] == "skip") finished[key] = 1
        }
        END { for (key in started) if (!(key in finished)) print key }
    ' "$log" | sort -u)
    if [ -z "$pending" ] || [ "$pending" = "$previous" ]; then
        break
    fi
    previous="$pending"
    for pkg in $(printf '%s\\n' "$pending" | cut -f1 | sort -u); do
        skip=$(printf '%s\\n' "$pending" | awk -F'\\t' -v p="$pkg" '$1 == p {print $2}' | paste -sd'|' -)
        go test -json -count=1 -timeout 60m -skip "^(${skip})\\$" "$pkg" | tee -a "$log" || rc=$?
    done
done
exit $rc
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
bash /home/guard_tests.sh
status=0
go test -json -count=1 -p 1 -timeout 60m ./... | tee /tmp/go-test.json || status=$?
bash /home/resume_tests.sh /tmp/go-test.json || status=$?
exit $status
"""


def _test_name(text: str) -> Optional[str]:
    match = _TEST_FUNC.match(text)
    if match and match.group(1) != "TestMain":
        return match.group(1)
    return None


def _changed_tests(test_patch: str) -> list[tuple[str, str]]:
    found: dict[tuple[str, str], None] = {}
    path: Optional[str] = None
    in_hunk = False
    current: Optional[str] = None
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            target = header.group(2)
            path = target if target.endswith("_test.go") else None
            in_hunk = False
            current = None
            continue
        if path is None:
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            in_hunk = True
            current = _test_name(hunk.group(1))
            continue
        if not in_hunk or not line or line[0] not in " +-":
            continue
        body = line[1:]
        if body.startswith("func "):
            current = _test_name(body)
        if line[0] != " " and current:
            found[(path, current)] = None
    return list(found)


def _test_targets(test_patch: str) -> str:
    rows = []
    for path, name in _changed_tests(test_patch):
        directory = posixpath.dirname(path) or "."
        rows.append(f"{directory}\t{path}\t{name}")
    return "".join(f"{row}\n" for row in rows)


def _run_script(repo: str, apply: str) -> str:
    return _RUN.replace("[[REPO]]", repo).replace("[[APPLY]]", apply)


class Delve4448To4181ImageBase(Image):
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
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
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
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

ENV CGO_ENABLED=1 \\
    GOTOOLCHAIN=local \\
    GOFLAGS=-buildvcs=false

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
    git ca-certificates build-essential dwz lsof procps \\
    && rm -rf /var/lib/apt/lists/*

RUN go version && gcc --version > /dev/null && dwz --version > /dev/null

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Delve4448To4181ImageDefault(Image):
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
        return Delve4448To4181ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        repo = self.pr.repo
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(
                ".",
                "prepare.sh",
                _PREPARE.replace("[[REPO]]", repo).replace(
                    "[[SHA]]", self.pr.base.sha
                ),
            ),
            File(".", "test_targets.txt", _test_targets(self.pr.test_patch)),
            File(".", "guard_tests.sh", _GUARD.replace("[[REPO]]", repo)),
            File(".", "resume_tests.sh", _RESUME.replace("[[REPO]]", repo)),
            File(".", "run.sh", _run_script(repo, "")),
            File(".", "test-run.sh", _run_script(repo, _APPLY_TEST)),
            File(".", "fix-run.sh", _run_script(repo, _APPLY_FIX)),
        ]

    def dockerfile(self) -> str:
        base = self.dependency()
        copy_commands = "".join(f"COPY {file.name} /home/\n" for file in self.files())
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
"""


@Instance.register("go-delve", _INTERVAL)
@Instance.register("go-delve", "delve_go1_24")
@Instance.register("go-delve", "delve_go1_23")
@Instance.register("go-delve", "delve_go1_22")
class DELVE_4448_TO_4181(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Delve4448To4181ImageDefault(self.pr, self._config)

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
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        started_tests: set[str] = set()
        for line in clean.splitlines():
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
            test = _VOLATILE_TIME.sub("time.Date(<time>)", test)
            test = _VOLATILE_ADDR.sub("0x<addr>", test)
            name = f"{package}::{test}"
            action = event.get("Action")
            if action == "run":
                started_tests.add(name)
            elif action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            elif action == "skip":
                skipped_tests.add(name)
        failed_tests |= started_tests - passed_tests - failed_tests - skipped_tests
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
