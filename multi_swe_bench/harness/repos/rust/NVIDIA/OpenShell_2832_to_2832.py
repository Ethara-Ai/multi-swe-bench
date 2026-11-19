import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "OpenShell_2832_to_2832"

# The graded tests are helm-unittest suites under deploy/helm/<chart>/tests (the repo's
# `helm:test` mise task). No Rust is built: the PR only changes the Helm chart.
# Official Debian image via its ECR Public mirror (multi-arch, no Hub pull-rate limits).
_DEBIAN_IMAGE = "public.ecr.aws/docker/library/debian:12.15-slim"
# mise.toml at the base sha pins helm = "4.2.0"; checksums from get.helm.sh/*.sha256sum.
_HELM_VERSION = "4.2.0"
_HELM_SHA256 = {
    "amd64": "97dbeb971be4ac4b27e3839976d9564c0fb35c6f3b1da89dd1e292d236af4096",
    "arm64": "1f8de130dfbd04de64978e7b852a7a547be1404956a366608276d2520b678670",
}
# tasks/helm.toml installs the plugin unpinned from git; pinned here to the release that
# was current at the base commit (2026-11-06): v1.2.1 (2026-10-03), checksums of the
# per-arch release tarballs.
_UNITTEST_VERSION = "1.2.1"
_UNITTEST_SHA256 = {
    "amd64": "48e86ecc1467b009630e5954375e5d8494139f73b466df5fe8c04fa45b634939",
    "arm64": "c66062713cd3cbb1a80d7098bd1d4a48db8438f56b43fb6377df65447f93f8de",
}

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_CHART_MARK = re.compile(r"^@@CHART (\S+)$")
_SUITE = re.compile(r"<testsuite\b[^>]*\bname=\"([^\"]*)\"")
_CASE = re.compile(r"<testcase\b([^>]*?)(/?)>")
_ATTR = re.compile(r"(\w+)=\"([^\"]*)\"")

_CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
    echo "check_git_changes: Not inside a git repository"
    exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
    echo "check_git_changes: Uncommitted changes"
    git status --porcelain
    exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

_PREPARE = """#!/bin/bash
set -euo pipefail

# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/[[REPO]]
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout --detach [[SHA]]
bash /home/check_git_changes.sh

# ---------- Section 2: PROVISION ----------
# The charts declare no dependencies at this commit (helm:test still runs
# `helm dependency build`, which is then a no-op); nothing else to fetch.
bash /home/select_charts.sh
while read -r c; do
    helm dependency build "$c"
done < /home/test_charts.txt

# ---------- Section 3: HARD GATE (non-tolerant) ----------
# The plugin resolves and every selected chart lints and renders with its defaults.
test -s /home/test_charts.txt
helm version
helm plugin list | grep -q unittest
helm unittest --help > /dev/null
while read -r c; do
    helm lint "$c" --set agentSandbox.preflight.enabled=false
    helm template gate "$c" --set agentSandbox.preflight.enabled=false > /dev/null
done < /home/test_charts.txt
echo DEPS_OK
"""

# A chart is selected when test.patch or fix.patch touches a file under it.
_SELECT = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
out=/home/test_charts.txt
: > "$out"
while IFS= read -r f; do
    [ -n "$f" ] || continue
    d=$(dirname "$f")
    while :; do
        if [ -f "$d/Chart.yaml" ]; then
            echo "$d" >> "$out"
            break
        fi
        [ "$d" = "." ] && break
        d=$(dirname "$d")
    done
done < /home/test_files.txt
sort -u -o "$out" "$out"
echo "selected charts:"
cat "$out"
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
bash /home/select_charts.sh
rc=0
while read -r c; do
    echo "@@CHART $c"
    rm -f /tmp/helm-unittest.xml
    helm unittest -t JUnit -o /tmp/helm-unittest.xml "$c" || rc=$?
    if [ -f /tmp/helm-unittest.xml ]; then
        cat /tmp/helm-unittest.xml
        echo
    else
        echo "@@NO_JUNIT_REPORT $c"
        rc=1
    fi
done < /home/test_charts.txt
echo "@@HELM_UNITTEST_EXIT $rc"
exit "$rc"
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _patch_files(patch: str) -> list[str]:
    found: dict[str, None] = {}
    for line in patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            found[header.group(2)] = None
    return list(found)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return template.replace("[[APPLY]]", apply).replace("[[REPO]]", pr.repo)


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


class OpenShell2832To2832ImageBase(Image):
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
        return _DEBIAN_IMAGE

    def image_tag(self) -> str:
        return f"base-{_INTERVAL}"

    def workdir(self) -> str:
        return f"base-{_INTERVAL}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        return f"""# syntax=docker/dockerfile:1.6

FROM {_DEBIAN_IMAGE}

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

ENV HELM_PLUGINS=/opt/helm/plugins \\
    HELM_CACHE_HOME=/opt/helm/cache \\
    HELM_CONFIG_HOME=/opt/helm/config \\
    HELM_DATA_HOME=/opt/helm/data

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
    git ca-certificates build-essential curl \\
    && rm -rf /var/lib/apt/lists/*

# helm (mise.toml pin) and the helm-unittest plugin, both checksum-pinned per arch. The
# plugin tarball already carries its binary, so it is unpacked rather than installed via
# its network-fetching install hook.
RUN set -eux; \\
    arch="${{TARGETARCH:-$(dpkg --print-architecture)}}"; \\
    case "$arch" in \\
        amd64) helm_sum={_HELM_SHA256["amd64"]}; ut_sum={_UNITTEST_SHA256["amd64"]} ;; \\
        arm64) helm_sum={_HELM_SHA256["arm64"]}; ut_sum={_UNITTEST_SHA256["arm64"]} ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-v{_HELM_VERSION}-linux-${{arch}}.tar.gz"; \\
    echo "${{helm_sum}}  /tmp/helm.tgz" | sha256sum -c -; \\
    tar -xzf /tmp/helm.tgz -C /tmp; \\
    install -m 0755 "/tmp/linux-${{arch}}/helm" /usr/local/bin/helm; \\
    rm -rf /tmp/helm.tgz "/tmp/linux-${{arch}}"; \\
    curl -fsSL -o /tmp/unittest.tgz "https://github.com/helm-unittest/helm-unittest/releases/download/v{_UNITTEST_VERSION}/helm-unittest-linux-${{arch}}-{_UNITTEST_VERSION}.tgz"; \\
    echo "${{ut_sum}}  /tmp/unittest.tgz" | sha256sum -c -; \\
    mkdir -p "$HELM_PLUGINS/helm-unittest"; \\
    tar -xzf /tmp/unittest.tgz -C "$HELM_PLUGINS/helm-unittest"; \\
    rm -f /tmp/unittest.tgz; \\
    helm version; \\
    helm plugin list | grep -q unittest

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com"

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class OpenShell2832To2832ImageDefault(Image):
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
        return OpenShell2832To2832ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        changed = _patch_files(pr.test_patch) + _patch_files(pr.fix_patch)
        test_files = "".join(f"{f}\n" for f in dict.fromkeys(changed))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "test_files.txt", test_files),
            File(".", "select_charts.sh", _render(_SELECT, pr)),
            File(".", "run.sh", _render(_RUN, pr)),
            File(".", "test-run.sh", _render(_RUN, pr, _APPLY_TEST)),
            File(".", "fix-run.sh", _render(_RUN, pr, _APPLY_FIX)),
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


@Instance.register("NVIDIA", _INTERVAL)
class OPENSHELL_2832_TO_2832(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return OpenShell2832To2832ImageDefault(self.pr, self._config)

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
        # helm-unittest JUnit: one <testsuite name="<suite>"> per test file, one
        # <testcase name="<it>"> per test; a child <failure>/<error> marks a failure and
        # <skipped> a skip. Names: <chart dir>::<suite name>::<test name>.
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        chart = "."
        suite = ""
        current: Optional[str] = None
        state = "pass"

        def close() -> None:
            if current is None:
                return
            if state == "fail":
                failed_tests.add(current)
            elif state == "skip":
                skipped_tests.add(current)
            else:
                passed_tests.add(current)

        for raw in clean.splitlines():
            line = raw.strip()
            mark = _CHART_MARK.match(line)
            if mark:
                close()
                current = None
                chart = mark.group(1)
                continue
            for token in re.split(r"(?=<)", line):
                if not token.startswith("<"):
                    continue
                m = _SUITE.match(token)
                if m:
                    close()
                    current = None
                    suite = _unescape(m.group(1))
                    continue
                m = _CASE.match(token)
                if m:
                    close()
                    attrs = dict(_ATTR.findall(m.group(1)))
                    name = _unescape(attrs.get("name", ""))
                    current = f"{chart}::{suite}::{name}" if name else None
                    state = "pass"
                    if m.group(2) == "/":
                        close()
                        current = None
                    continue
                if current is not None and (
                    token.startswith("<failure") or token.startswith("<error")
                ):
                    state = "fail"
                elif current is not None and token.startswith("<skipped"):
                    if state != "fail":
                        state = "skip"
                elif token.startswith("</testcase"):
                    close()
                    current = None
                elif token.startswith("</testsuite"):
                    close()
                    current = None
        close()
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
