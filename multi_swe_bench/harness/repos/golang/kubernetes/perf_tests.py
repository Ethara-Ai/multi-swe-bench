from __future__ import annotations

import json
import re
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# SHARED BUILD BLOCKS — inlined so this config is self-contained, matching every other file
# under harness/repos/**. The architecture is fixed by req.txt:
#   1. the BASE Dockerfile carries content only up to `git clone`, then CMD ["/bin/bash"];
#   2. git stripping/hardening lives in the PR Dockerfile — never in the base, never in
#      prepare.sh.
# That is the shared-base shape of QC_PROMPT_BASE_PR_PREPARE.md (Reference A).
#
# The prune block below ships FOUR canonical assertions (HEAD, refs, remotes, rev-list) and
# BOTH `git reflog expire` variants. Deleting refs alone does not delete commits — the reflog
# is itself a reachability root — so dropping either expire line silently leaks the fix
# commit into a shipped image that still builds and still passes.
# ---------------------------------------------------------------------------
# The DockerfileEnhancer opt-out (image.py:317). Load-bearing on a shared base: without it
# `_standardize_repo_fetch` rewrites the clone into `git checkout ${BASE_COMMIT}` + the
# hardening block, pinning the shared base to whichever PR built it first and breaking every
# other PR in the shard — while the committed config still looks correct.
SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="

# Stripped before any matching. `go test -json` emits structured JSON and this parser reads
# only Action/Test/Package, so ANSI is not expected here — but a colour sequence leaking onto
# a line would defeat the `startswith("{")` guard below and silently drop that record, and
# Check 4C requires the strip unconditionally.
_ANSI_RE = re.compile(r"\x1B\[[0-?9;]*[mK]")


def _arg_env_label(org: str, repo: str) -> str:
    return f'''ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT
# ^ Declared, never referenced. The harness passes BASE_COMMIT to every base build
#   (build_dataset.py:612-619); declaring it silences BuildKit's unused-arg warning, while
#   CONSUMING it is what would pin this shared base to a single PR.

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
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''


def apt_block(packages: list[str], *, bullseye: bool = False) -> str:
    """An apt install, optionally with the Debian-bullseye archive workaround.

    `bullseye=True` drops the security suite first. Bullseye's security pool has been
    pruned while its index still advertises the removed .debs, so *every* `apt-get install`
    on a bullseye image 404s — reproducible on a stock `python:3.8-slim-bullseye`, nothing
    to do with any repo. Dropping the suite resolves the same packages from the main
    bullseye pool one security revision older, which is correct for an image pinned to a
    historical source tree anyway.

    `Acquire::Retries` is unconditional: a transient CDN drop mid-download has failed a
    base build here with "Error reading from server. Remote end closed connection".
    """
    pkgs = " \\\n    ".join(sorted(packages))
    sed = (
        "sed -i '/security.debian.org/d; /debian-security/d' /etc/apt/sources.list \\\n    && "
        if bullseye
        else ""
    )
    return (
        f"RUN {sed}apt-get -o Acquire::Retries=5 update \\\n"
        f"    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \\\n"
        f"    {pkgs} \\\n"
        f"    && rm -rf /var/lib/apt/lists/*"
    )


def base_dockerfile(
    pr: PullRequest,
    image_name: str,
    *,
    apt_packages: list[str] | None = None,
    bullseye: bool = False,
    extra_env: str = "",
    extra_run: str = "",
) -> str:
    """Render the shared base: everything up to the clone, then CMD (req.txt #1)."""
    sections = [
        f"{SYNTAX_DIRECTIVE}\nFROM {image_name}",
        _arg_env_label(pr.org, pr.repo),
    ]
    if extra_env:
        sections.append(extra_env)
    if apt_packages:
        sections.append(apt_block(apt_packages, bullseye=bullseye))
    if extra_run:
        sections.append(extra_run)
    # git >= 2.35.2 refuses to operate on a tree owned by another uid; the graded stages run
    # as root over a tree written at build time, so declare it safe once here.
    sections.append("RUN git config --global --add safe.directory '*'")
    sections.append("WORKDIR /home/")
    sections.append(
        "# Full-history clone, kept intact: NO checkout and NO scrub here — both are\n"
        "# per-PR and belong to the PR layer (req.txt #2).\n"
        f'RUN git clone "${{REPO_URL}}" /home/{pr.repo} \\\n'
        f"    && cd /home/{pr.repo} \\\n"
        "    && git rev-parse HEAD >/dev/null"
    )
    sections.append('CMD ["/bin/bash"]')
    return "\n\n".join(sections) + "\n"


def _prune_block(repo: str, sha: str) -> str:
    """The git stripping — owned by the PR layer per req.txt #2.

    Four canonical assertions (HEAD, refs, remotes, rev-list) and BOTH `git reflog expire`
    variants: deleting refs alone does not delete commits, because the reflog is itself a
    reachability root and every fix commit would stay recoverable.

    Deliberately contains no `git reset`, no `git clean` and no path-scoped checkout.
    prepare.sh is permitted to leave the tree intentionally dirty (a patched test config, a
    generated version file, a stubbed conftest) and any of those commands would silently
    revert exactly that work, surfacing much later as unrelated test errors.
    """
    return f'''RUN set -eux; \\
    cd /home/{repo}; \\
    test "$(git rev-parse HEAD)" = "{sha}"; \\
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
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

# Submodules carry their own history and their own leak. No-op when absent.
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
    fi'''


def pr_dockerfile(pr: PullRequest, base_full_name: str, files) -> str:
    """Render the PR layer. Never enhanced — `enhance()` returns raw the moment the
    dependency is an Image rather than a str (image.py:315-316) — so everything this layer
    needs is written out here and nothing is injected."""
    copy_commands = "".join(f"COPY {f.name} /home/\n" for f in files)
    return f"""FROM {base_full_name}

{copy_commands}
# BUILD time: prepare.sh pins this PR's base commit and installs the era's dependencies, so
# the shipped image is already provisioned before the agent starts.
RUN bash /home/prepare.sh

{_prune_block(pr.repo, pr.base.sha)}
"""


CHECK_GIT_CHANGES = """#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""


def pin_section(repo: str, sha: str) -> str:
    """Section 1 of prepare.sh: make the tree pristine, assert it, land detached on this
    PR's base commit, assert again.

    The `.git/info/attributes` write is what MAKES the tree pristine on a repo that ships
    `.gitattributes` with `text`/`eol` rules. `git apply` matches patch context against BLOB
    bytes, but an eol filter produces a working tree that differs from the index by line
    endings alone — a tree that reads as modified at HEAD no matter how often it is reset,
    so the pristine assertion below can never pass and CRLF hunks fail to apply.
    lemon24/reader hits this squarely (`*.bat text eol=crlf` plus an unnormalised
    docs/make.bat that the gold fix_patch rewrites). A harmless no-op everywhere else.
    """
    return f"""# ---------- Section 1: PIN the tree to this PR's base commit ----------
cd /home/{repo}

git reset --hard
git clean -fdx
printf '* -text\\n' > .git/info/attributes
git checkout-index -a -f
bash /home/check_git_changes.sh          # ASSERT pristine BEFORE the pin

git checkout --detach {sha}
bash /home/check_git_changes.sh          # ASSERT pristine AT the base commit
"""


def prepare_sh(repo: str, sha: str, *, provision: str, gate: str, env: str = "") -> str:
    """Assemble the three canonical sections. No patch is ever applied here (that is what
    the three graded stages are for) and no history is scrubbed (req.txt #2)."""
    head = "#!/bin/bash\nset -euo pipefail\n\n"
    if env:
        head += env.rstrip("\n") + "\n\n"
    return (
        head
        + pin_section(repo, sha)
        + "\n# ---------- Section 2: PROVISION the era's dependencies, at BUILD time ----------\n"
        + "# From here on the tree may be INTENTIONALLY dirty — no clean-tree assertion below.\n"
        + provision.rstrip("\n")
        + "\n\n# ---------- Section 3: HARD GATE — last, and not tolerant of failure ----------\n"
        + "# A tree that cannot import or collect produces no results in ANY stage; that must\n"
        + "# fail HERE, not surface as an unexplained empty report three stages later.\n"
        + gate.rstrip("\n")
        + '\n\necho "DEPS_OK"\n'
    )


def stage_scripts(repo: str) -> dict[str, str]:
    """run.sh / test-run.sh / fix-run.sh — the three graded stages.

    All three delegate to the single run_tests.sh so the test command cannot drift between
    stages; they differ only in which patches are applied first. A failed `git apply` is a
    hard error: silently grading an unpatched tree would report a clean run and destroy the
    f2p signal.
    """
    common = f"#!/bin/bash\nset -eo pipefail\n\ncd /home/{repo}\n"
    return {
        "run.sh": common + "bash /home/run_tests.sh\n",
        "test-run.sh": common
        + "if ! git apply --whitespace=nowarn /home/test.patch; then\n"
        '    echo "Error: git apply test.patch failed" >&2\n'
        "    exit 1\n"
        "fi\n"
        "bash /home/run_tests.sh\n",
        "fix-run.sh": common
        + "if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then\n"
        '    echo "Error: git apply test.patch+fix.patch failed" >&2\n'
        "    exit 1\n"
        "fi\n"
        "bash /home/run_tests.sh\n",
    }


def run_tests_sh(
    repo: str,
    test_cmd: str,
    *,
    env: str = "",
    prefix: str = "",
    go: bool = False,
    extra_go_module: str = "",
) -> str:
    """The ONE place the suite is invoked; all three stages delegate here.

    `set -e` is lifted only around the test call: at the test stage the suite is SUPPOSED to
    fail, and dying before the results are printed would report zero tests and satisfy
    report.py's "the fix must fix something" check vacuously.
    """
    out = "#!/bin/bash\nset -eo pipefail\n\nexport CI=true\n"
    if env:
        out += env.rstrip("\n") + "\n"
    out += "\n"
    if prefix:
        out += prefix.rstrip("\n") + "\n\n"
    out += f"cd /home/{repo}\n\n"
    out += "# never inherit the previous stage's results\n"
    out += (
        "rm -f /home/gotest.json /home/gotest.err\n\n"
        if go
        else "rm -f /home/results.xml\n\n"
    )
    out += "set +e\n"
    if go:
        out += f"{test_cmd} > /home/gotest.json 2> /home/gotest.err\n"
        out += "RC=$?\n"
        if extra_go_module:
            out += (
                f"if [ -f /home/{repo}/{extra_go_module}/go.mod ]; then\n"
                f"  (cd /home/{repo}/{extra_go_module} && {test_cmd}) \\\n"
                "    >> /home/gotest.json 2>> /home/gotest.err\n"
                "fi\n"
            )
    else:
        out += f"{test_cmd}\nRC=$?\n"
    out += "set -e\n"
    out += 'echo "TEST_EXIT_CODE=$RC"\n\n'
    if go:
        out += (
            "# Compile errors land on stderr and never reach the JSON stream; echoing them\n"
            "# keeps the reason a package reported zero tests in the graded log rather than\n"
            "# only in a lost file descriptor.\n"
            'echo "===== go test stderr ====="\n'
            "cat /home/gotest.err || true\n\n"
            f'echo "{BEGIN_MARKER}"\n'
            "cat /home/gotest.json || true\n"
            f'echo "{END_MARKER}"\n'
        )
    else:
        out += (
            f'echo "{BEGIN_MARKER}"\n'
            "cat /home/results.xml || true\n"
            f'echo "{END_MARKER}"\n'
        )
    return out


# ---------------------------------------------------------------------------
# kubernetes/perf-tests — a Go monorepo with SEVEN independent modules (clusterloader2,
# perfdash, network, slo-monitor, _logviewer, util-images/*). There is no root go.mod, so
# `go test ./...` at the repo root is meaningless.
#
# Architecture: shared base (req.txt / QC Reference A) — see the SHARED BUILD BLOCKS section below.
#
# Scope: the clusterloader2 module. That is where the gold test_patch's only _test.go file
# lives (pkg/measurement/common/slos/api_responsiveness_prometheus_test.go) and it holds all
# nine unit-test files in the tree. The patch's second file,
# network/benchmarks/netperf/nptest/nptest.go, is production code that the collector's path
# heuristic filed under test_patch because its NAME contains "test"; it carries no tests and
# is never compiled here.

# clusterloader2/go.mod declares `go 1.13` and replaces the whole k8s tree at v0.18.0 (March
# 2020). Go 1.16 flipped the -mod=vendor/GO111MODULE defaults and started rejecting the
# implicit-dependency patterns this era relies on, so the toolchain follows the tree rather
# than the calendar. 1.15 is the last release in that band and the last with a `-buster`
# variant.
LANG_IMAGE = "golang:1.15-buster"

MODULE_DIR = "clusterloader2"

# -json   : machine-readable, package-qualified records. Plain `--- PASS: TestX` console
#           lines carry no package, and this module has nine test packages that can and do
#           repeat a test name.
# -count=1: defeat the build cache, so stage N does not replay stage N-1's verdicts.
TEST_CMD = f"cd /home/perf-tests/{MODULE_DIR} && go test -json -count=1 ./..."

# clusterloader2 vendors its full k8s v0.18 dependency tree. Resolving from the network
# instead would make a 2020 module graph a live dependency of every run.
GO_ENV = """export GO111MODULE=on
export GOFLAGS=-mod=vendor
export GOPROXY=off
export GOSUMDB=off"""

PROVISION = f"""{GO_ENV}

cd /home/perf-tests/{MODULE_DIR}
test -d vendor
go build ./..."""

# `go vet` is deliberately not run: the gold fix_patch is an errcheck-linter sweep, so a vet
# finding is the thing under test, not a build prerequisite.
GATE = f"""cd /home/perf-tests/{MODULE_DIR} \\
    && go test -count=1 -run XXX_NO_MATCH ./... > /dev/null"""


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

    def dependency(self) -> str:
        return LANG_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # No apt block: the full golang image already carries git, ca-certificates, make and
        # a C toolchain, and buster's repositories have moved to archive.debian.org — an
        # `apt-get update` here would be a live network dependency on a deprecated mirror
        # for packages that are already present.
        return base_dockerfile(self.pr, self.dependency())


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

    def dependency(self) -> Image:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        stages = stage_scripts(self.pr.repo)
        return [
            File(".", "fix.patch", self.pr.fix_patch),
            File(".", "test.patch", self.pr.test_patch),
            File(".", "check_git_changes.sh", CHECK_GIT_CHANGES),
            File(
                ".",
                "run_tests.sh",
                run_tests_sh(self.pr.repo, TEST_CMD, env=GO_ENV, go=True),
            ),
            File(
                ".",
                "prepare.sh",
                prepare_sh(
                    self.pr.repo, self.pr.base.sha, provision=PROVISION, gate=GATE
                ),
            ),
            File(".", "run.sh", stages["run.sh"]),
            File(".", "test-run.sh", stages["test-run.sh"]),
            File(".", "fix-run.sh", stages["fix-run.sh"]),
        ]

    def dockerfile(self) -> str:
        return pr_dockerfile(self.pr, self.dependency().image_full_name(), self.files())


# One PR in this dataset (#1426), so there is no interval to name. Instance.create
# derives the key from {org}/{repo} when number_interval is unset
# (instance.py:41-51), so "kubernetes/perf-tests" is the only key a record here can resolve to.
@Instance.register("kubernetes", "perf-tests")
class KUBERNETES_PERF_TESTS(Instance):
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
        return run_cmd or "bash /home/run.sh"

    def test_patch_run(self, test_patch_run_cmd: str = "") -> str:
        return test_patch_run_cmd or "bash /home/test-run.sh"

    def fix_patch_run(self, fix_patch_run_cmd: str = "") -> str:
        return fix_patch_run_cmd or "bash /home/fix-run.sh"

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Only the `go test -json` stream between the markers is parsed: the stderr block
        # echoed just above it carries compiler diagnostics whose text can contain anything,
        # including strings that look like results.
        in_detail = False
        for line in _ANSI_RE.sub("", test_log).splitlines():
            stripped = line.strip()
            if stripped.startswith(BEGIN_MARKER):
                in_detail = True
                continue
            if stripped.startswith(END_MARKER):
                in_detail = False
                continue
            if not in_detail or not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("Action") not in ("pass", "fail", "skip"):
                continue
            # Package-level roll-ups carry no "Test" key; counting them would add one
            # synthetic result per package on top of the real tests.
            test = event.get("Test")
            if not test:
                continue
            # Package-qualified: Go repos of this size repeat test names across packages,
            # and an unqualified name would collide between them.
            package = event.get("Package") or ""
            name = f"{package}::{test}" if package else test

            action = event["Action"]
            if action == "pass":
                passed_tests.add(name)
            elif action == "fail":
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # Failure wins; each test lands in exactly one bucket. A parent test emits its own
        # pass/fail alongside its subtests, and a parent can fail while some subtests pass.
        passed_tests -= failed_tests
        skipped_tests -= passed_tests
        skipped_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
