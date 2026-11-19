import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

_INTERVAL = "devenv_3259_to_3259"

# The graded tests are devenv integration tests (tests/<name>/devenv.nix), run upstream
# by `devenv-run-tests run tests --only <name>` (.github/workflows/test.yml) with the
# devenv package built from the checked-out flake (`nix build`). That needs Nix, so the
# image carries a single-user Nix: 2.35.2, the version cachix/install-nix-action@v31
# (v31.11.1) installed when the base commit landed (2026-10-09). devenv itself, and
# the nixpkgs revision the tests evaluate against (flake.lock's `nixpkgs` node, read by
# devenv-run-tests), come from this commit's flake.lock.
# Official Debian image via its ECR Public mirror (multi-arch, no Hub pull-rate limits).
_DEBIAN_IMAGE = "public.ecr.aws/docker/library/debian:12.15-slim"
_NIX_VERSION = "2.35.2"
_NIX_SHA256 = {
    "x86_64": "0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158",
    "aarch64": "4d0302a2910f5eec1c33b8deef634f04899a75737e7001ec49908d003ae5efda",
}
# Substituters/keys from the flake's own nixConfig (devenv.cachix.org) plus the default
# cache; both are public.
_NIX_CONF = """build-users-group =
sandbox = false
filter-syscalls = false
experimental-features = nix-command flakes
substituters = https://cache.nixos.org https://devenv.cachix.org
trusted-public-keys = cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=
max-jobs = 2
cores = 2
accept-flake-config = false
warn-dirty = false
"""
# Existing tiny test (`{ }`) run by the build-time gate: proves the toolchain works end
# to end and pre-fetches the shared shell closure the graded tests reuse.
_GATE_TEST = "exit-code"
# Scratch devenv environments (outside the repo, not derived from either patch) that
# pull the BEAM runtimes from the nixpkgs devenv-run-tests evaluates against: the default
# `languages.elixir` package set and the beam27 one. Run once at build time so the graded
# runs find those closures in the local store.
_PREFETCH_ENVS = {
    "prefetch-elixir-default": "{ languages.elixir.enable = true; languages.elixir.lsp.enable = false; }",
    "prefetch-elixir-beam27": "{ pkgs, ... }: { languages.elixir.enable = true; languages.elixir.lsp.enable = false; languages.elixir.package = pkgs.beam27Packages.elixir; }",
}
_RUN_TIMEOUT = "2700"

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_FILE_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_RESULT = re.compile(r"\[\d+/\d+\] (Passed|Failed): (\S+)")
_SUMMARY_HEAD = re.compile(r"Test results:\s*$")
_SUMMARY_ROW = re.compile(r"^(passed|FAILED|skipped)\s+(\S+)")

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

# ---------- Section 2: PROVISION (pinned by this commit's flake.lock) ----------
# devenv + devenv-run-tests exactly as CI builds them (`nix build`, default package).
nix build -L --print-out-paths --out-link /opt/devenv-cli .
# BEAM runtimes from the nixpkgs devenv-run-tests evaluates against (scratch envs).
rm -rf /tmp/devenv-prefetch
[[PREFETCH_SETUP]]/opt/devenv-cli/bin/devenv-run-tests run /tmp/devenv-prefetch
rm -rf /tmp/devenv-prefetch

# ---------- Section 3: HARD GATE (non-tolerant) ----------
/opt/devenv-cli/bin/devenv version
/opt/devenv-cli/bin/devenv-run-tests --help > /dev/null
/opt/devenv-cli/bin/devenv-run-tests run tests --only [[GATE_TEST]]
echo DEPS_OK
"""

_RUN = """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/[[REPO]]
[[APPLY]]
bash /home/devenv_tests.sh
"""

# Runs each devenv test directory test.patch touches that exists at this stage.
_DEVENV_TESTS = """#!/bin/bash
set -eo pipefail

cd /home/[[REPO]]
args=()
while IFS= read -r t; do
    [ -n "$t" ] || continue
    if [ -f "tests/$t/devenv.nix" ]; then
        args+=(--only "$t")
    else
        echo "@@NOT_PRESENT $t"
    fi
done < /home/devenv_test_dirs.txt
if [ "${#args[@]}" -eq 0 ]; then
    echo "@@NO_DEVENV_TESTS_AT_THIS_STAGE"
    exit 0
fi
rc=0
timeout -k 60 [[RUN_TIMEOUT]] /opt/devenv-cli/bin/devenv-run-tests run tests "${args[@]}" 2>&1 || rc=$?
echo "@@DEVENV_RUN_TESTS_EXIT $rc"
exit "$rc"
"""

_APPLY_TEST = "git apply --whitespace=nowarn /home/test.patch"
_APPLY_FIX = (
    "git apply --whitespace=nowarn /home/test.patch\n"
    "git apply --whitespace=nowarn /home/fix.patch"
)


def _test_dirs(test_patch: str) -> list[str]:
    found: dict[str, None] = {}
    for line in test_patch.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            parts = header.group(2).split("/")
            if len(parts) >= 3 and parts[0] == "tests":
                found[parts[1]] = None
    return list(found)


def _prefetch_setup() -> str:
    lines = []
    for name, body in _PREFETCH_ENVS.items():
        lines.append(f"mkdir -p /tmp/devenv-prefetch/{name}")
        lines.append(f"cat > /tmp/devenv-prefetch/{name}/devenv.nix <<'NIX'\n{body}\nNIX")
    return "".join(f"{line}\n" for line in lines)


def _render(template: str, pr: PullRequest, apply: str = "") -> str:
    return (
        template.replace("[[APPLY]]", apply)
        .replace("[[PREFETCH_SETUP]]", _prefetch_setup())
        .replace("[[GATE_TEST]]", _GATE_TEST)
        .replace("[[RUN_TIMEOUT]]", _RUN_TIMEOUT)
        .replace("[[REPO]]", pr.repo)
    )


class Devenv3259To3259ImageBase(Image):
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
        nix_conf = "\\n".join(_NIX_CONF.strip().split("\n"))
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

ENV USER=root \\
    HOME=/root \\
    NIX_SSL_CERT_FILE=${{CA_CERT_PATH}} \\
    PATH=/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:${{PATH}}

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
    git ca-certificates build-essential curl xz-utils python3 procps \\
    && rm -rf /var/lib/apt/lists/*

# Single-user Nix, checksum-pinned per architecture (sandbox off: no user namespaces
# inside an unprivileged build container).
RUN set -eux; \\
    case "${{TARGETARCH:-$(dpkg --print-architecture)}}" in \\
        amd64) sys=x86_64; sum={_NIX_SHA256["x86_64"]} ;; \\
        arm64) sys=aarch64; sum={_NIX_SHA256["aarch64"]} ;; \\
        *) echo "unsupported architecture" >&2; exit 1 ;; \\
    esac; \\
    mkdir -p /etc/nix; \\
    mkdir -m 0755 /nix; \\
    printf '{nix_conf}\\n' > /etc/nix/nix.conf; \\
    curl -fsSL -o /tmp/nix.tar.xz "https://releases.nixos.org/nix/nix-{_NIX_VERSION}/nix-{_NIX_VERSION}-${{sys}}-linux.tar.xz"; \\
    echo "${{sum}}  /tmp/nix.tar.xz" | sha256sum -c -; \\
    mkdir -p /tmp/nix-install; \\
    tar -xJf /tmp/nix.tar.xz -C /tmp/nix-install --strip-components=1; \\
    /tmp/nix-install/install --no-daemon --no-channel-add --no-modify-profile; \\
    rm -rf /tmp/nix.tar.xz /tmp/nix-install; \\
    nix --version

RUN git config --global --add safe.directory '*' && \\
    git config --global http.version HTTP/1.1 && \\
    git config --global user.name "mswebench" && \\
    git config --global user.email "mswebench@example.com" && \\
    git config --global init.defaultBranch main

WORKDIR /home/

RUN git clone "${{REPO_URL}}" /home/{self.pr.repo} && \\
    cd /home/{self.pr.repo} && git rev-parse HEAD >/dev/null

CMD ["/bin/bash"]
"""


class Devenv3259To3259ImageDefault(Image):
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
        return Devenv3259To3259ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        pr = self.pr
        prepare = _render(_PREPARE, pr).replace("[[SHA]]", pr.base.sha)
        dirs = "".join(f"{d}\n" for d in _test_dirs(pr.test_patch))
        return [
            File(".", "fix.patch", f"{pr.fix_patch}"),
            File(".", "test.patch", f"{pr.test_patch}"),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", prepare),
            File(".", "devenv_test_dirs.txt", dirs),
            File(".", "devenv_tests.sh", _render(_DEVENV_TESTS, pr)),
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


@Instance.register("cachix", _INTERVAL)
class DEVENV_3259_TO_3259(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return Devenv3259To3259ImageDefault(self.pr, self._config)

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
        # devenv-run-tests prints "<icon> [i/n] Passed: <dir> (stats)" per test and a final
        # "Test results:" table ("passed|FAILED|skipped  <dir>  ..."); names are the test's
        # devenv.nix path (stable, and the file test.patch adds).
        clean = _ANSI.sub("", test_log)
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        in_summary = False
        for raw in clean.splitlines():
            line = raw.strip()
            if _SUMMARY_HEAD.search(line):
                in_summary = True
                continue
            m = _RESULT.search(line)
            if m:
                name = f"tests/{m.group(2)}/devenv.nix"
                (passed_tests if m.group(1) == "Passed" else failed_tests).add(name)
                continue
            row = _SUMMARY_ROW.match(line) if in_summary else None
            if row:
                name = f"tests/{row.group(2)}/devenv.nix"
                status = row.group(1)
                if status == "passed":
                    passed_tests.add(name)
                elif status == "FAILED":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
            elif in_summary and line and not line.startswith(("Ran ", "•", "✖", "✓")):
                in_summary = False
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
