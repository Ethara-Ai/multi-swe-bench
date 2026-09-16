from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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


_ANSI_RE = re.compile(r"\x1B\[[0-?9;]*[mK]")


# ---------------------------------------------------------------------------
# kr8s-org/kr8s — an async Kubernetes API library.
#
# Architecture: shared base (req.txt / QC Reference A) — see the SHARED BUILD BLOCKS section below.
#
# Scope: the single file the gold test_patch adds. The rest of kr8s/tests talks to a live
# cluster (see CONFTEST_STUB) and cannot run in a grading container.

# pyproject declares requires-python >=3.8; ruff targets py310. The runtime deps of this era
# (httpx-ws>=0.5.1, python-box>=7, anyio>=3.7) are all wheels on 3.11, the newest interpreter
# this 2024-04 tree was exercised against.
LANG_IMAGE = "python:3.11-slim-bookworm"

APT = ["bash", "ca-certificates", "git"]

# hatch-vcs derives the package version from git tags. HEAD is detached by the time
# prepare.sh installs, and the PR layer's prune block later deletes every ref, so leaving
# version discovery to git invites "unable to determine version". hatch-vcs delegates to
# setuptools_scm, so both pretend-version variables are honoured. Cosmetic: nothing in the
# suite asserts on kr8s.__version__.
PRETEND_VERSION = "0.14.0"

# --junitxml    : machine-readable; see parse_junit_log below.
# --override-ini: pyproject's addopts carry `--keep-cluster` (a pytest-kind option) and
#                 `--cov`; with neither plugin installed an unknown option is a hard usage
#                 error, not a warning.
# --continue-on-collection-errors : at the test stage `kr8s._config` does not exist yet, so
#                 this module is SUPPOSED to fail to import.
TEST_CMD = (
    "python -m pytest kr8s/tests/test_config.py -v --tb=short "
    "--override-ini=addopts= -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/results.xml"
)

# The upstream root conftest.py stands up a real KIND cluster (Docker-in-Docker) in a
# session-scoped autouse fixture, and kr8s/conftest.py hangs an autouse `ns` fixture off it
# that shells out to kubectl. Neither is available inside a grading container, and both run
# for ANY test collected under the repo root.
#
# The graded tests do not need a cluster: `temp_kubeconfig` only copies the BYTES of
# k8s_cluster.kubeconfig_path, and KubeConfig/KubeConfigSet are pure file+YAML
# (anyio.open_file + yaml.safe_load / safe_dump) with no socket in sight. So the fixture is
# replaced with a static kubeconfig on disk and a no-op kubectl. This is a harness
# substitution, not a test rewrite: the assertions, the module under test and the three-stage
# comparison are untouched, and the substitution is identical across all three stages.
CONFTEST_STUB = '''# Replaced by the Multi-SWE-Bench harness. See the config module for the
# rationale: the graded kubeconfig tests parse a file and never contact a cluster, so the
# KIND (Docker-in-Docker) session fixture is stubbed out.
import os

import pytest

KUBECONFIG = """apiVersion: v1
kind: Config
preferences: {}
extensions: []
current-context: pytest-kind-cluster
clusters:
- name: pytest-kind-cluster
  cluster:
    server: https://127.0.0.1:6443
    certificate-authority-data: RkFLRS1DQQ==
contexts:
- name: pytest-kind-cluster
  context:
    cluster: pytest-kind-cluster
    user: pytest-kind-cluster
users:
- name: pytest-kind-cluster
  user:
    client-certificate-data: RkFLRS1DRVJU
    client-key-data: RkFLRS1LRVk=
"""


class StubCluster:
    """Stands in for pytest_kind.cluster.KindCluster.

    Only the surface the suite actually touches: ``kubeconfig_path`` (read by
    temp_kubeconfig) and ``kubectl`` (called by the autouse ``ns`` fixture in
    kr8s/conftest.py, which creates and deletes a namespace nothing graded looks at).
    """

    def __init__(self, kubeconfig_path):
        self.kubeconfig_path = kubeconfig_path
        self.kubectl_path = "/bin/true"

    def kubectl(self, *args, **kwargs):
        return ""


@pytest.fixture(scope="session", autouse=True)
def k8s_cluster(tmp_path_factory):
    path = tmp_path_factory.mktemp("kubeconfig") / "config"
    path.write_text(KUBECONFIG)
    os.environ["KUBECONFIG"] = str(path)
    yield StubCluster(path)
    os.environ.pop("KUBECONFIG", None)
'''

# asyncio_mode=auto in pyproject means the bare `async def` tests need pytest-asyncio;
# without it they are collected, skipped with a warning, and every stage reports the same
# nothing.
#
# sniffio is a genuine UNDECLARED dependency: kr8s/_portforward.py imports it directly but
# pyproject never lists it. In 2024 it arrived transitively via anyio; anyio dropped that
# requirement later (4.15.1 requires only exceptiongroup/idna/typing_extensions), so
# resolving this unpinned tree today yields an anyio without sniffio and `import kr8s` dies
# with ModuleNotFoundError.
#
# trio is needed only so the gate can sweep the whole kr8s/tests directory: test_io.py
# imports it at module scope, and without it that one module raises during collection and
# pytest exits 2 even though 122 other tests collect fine. It is a declared [test] extra, so
# installing it is faithful rather than a workaround.
PROVISION = f"""export SETUPTOOLS_SCM_PRETEND_VERSION="{PRETEND_VERSION}"
export HATCH_VCS_PRETEND_VERSION="{PRETEND_VERSION}"

pip install --no-cache-dir pytest pytest-asyncio sniffio trio
pip install --no-cache-dir -e /home/kr8s

cp /home/conftest_stub.py /home/kr8s/conftest.py"""

# test_config.py does not exist at the base commit (the gold test_patch adds it), so the gate
# sweeps the suite directory instead — which is also what proves the stubbed conftest imports
# cleanly.
GATE = """python -c "import kr8s, anyio, yaml, pytest; print('imports ok')"
cd /home/kr8s && python -m pytest kr8s/tests --collect-only -q \\
    --override-ini=addopts= -p no:cacheprovider"""

# Re-applied every stage: a stage that silently ran against the real KIND fixture would hang,
# not fail loudly.
RUN_PREFIX = "cp /home/conftest_stub.py /home/kr8s/conftest.py"


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
        return base_dockerfile(
            self.pr,
            self.dependency(),
            apt_packages=APT,
            extra_run="RUN pip install --no-cache-dir --upgrade pip setuptools wheel",
        )


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
            File(".", "conftest_stub.py", CONFTEST_STUB),
            File(
                ".",
                "run_tests.sh",
                run_tests_sh(self.pr.repo, TEST_CMD, prefix=RUN_PREFIX),
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


# One PR in this dataset (#347), so there is no interval to name. Instance.create
# derives the key from {org}/{repo} when number_interval is unset
# (instance.py:41-51), so "kr8s-org/kr8s" is the only key a record here can resolve to.
@Instance.register("kr8s-org", "kr8s")
class KR8S_ORG_KR8S(Instance):
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

        # run_tests.sh cats the junit XML between the markers and this parses it directly —
        # the same job the old shipped parse_junit.py did inside the container, moved here so
        # it is ordinary code rather than a Python string rendered to a file and run by a
        # shell. Console `-v` text is still never parsed: pytest's short summary prints
        # `FAILED tests/x.py::test_y - AssertionError: ...`, and a regex over that captures
        # the error message INTO the test id, so the same test would get a different name at
        # the test stage than at the fix stage and the transition would be silently lost.
        text = _ANSI_RE.sub("", test_log)
        if BEGIN_MARKER not in text or END_MARKER not in text:
            return TestResult(0, 0, 0, set(), set(), set())
        xml = text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            # A truncated or interleaved document yields no results rather than a crash;
            # Report.check rule 1 then rejects the instance loudly instead of scoring a
            # partial read.
            return TestResult(0, 0, 0, set(), set(), set())

        for tc in root.iter("testcase"):
            # @file gives a real, rerunnable node id (tests/x.py::test_y[param]); classname
            # is the fallback for runners that omit it.
            path = tc.get("file")
            if not path:
                classname = tc.get("classname") or ""
                path = classname.replace(".", "/") + ".py"
            # Newlines are flattened to keep ids byte-identical to what the previous
            # line-oriented parser produced, so verdicts do not shift on this change.
            name = (tc.get("name") or "").replace("\r", " ").replace("\n", " ")

            status = "PASSED"
            for child in tc:
                if child.tag in ("failure", "error"):
                    status = "FAILED"
                    break
                if child.tag == "skipped":
                    status = "SKIPPED"
                    break

            full = path + "::" + name
            if status == "PASSED":
                passed_tests.add(full)
            elif status == "FAILED":
                failed_tests.add(full)
            else:
                skipped_tests.add(full)

        # Failure wins; each test lands in exactly one bucket. TestResult.__post_init__
        # rejects any overlap outright, so this normalisation is load-bearing, not defensive.
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
