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
# asottile/pyupgrade — a source-rewriting tool. Plain pytest, no services.
#
# Architecture: shared base (req.txt / QC Reference A) — see the SHARED BUILD BLOCKS section below.

# ERA ANALYSIS — this config spans PRs #371..#885, a range of 514, so the two endpoints were
# compared at their base commits rather than assumed equivalent:
#
#   PR #371  (base 1dcc726e, 2020)  setup.cfg classifiers 3.6-3.9,  tokenize-rt < 5
#   PR #885  (base 3bbf7817, 2023)  setup.cfg python_requires >=3.8.1, tokenize-rt >= 5.2.0
#
# The build system does NOT migrate across that range: both are setup.cfg + setup.py driven,
# installed with pip, tested with pytest. Only the interpreter floor and one dependency pin
# move, and a single python:3.8 image satisfies both (3.8 sits inside 3.6-3.9 and satisfies
# >=3.8.1). The dependency difference resolves itself per era because `pip install -e .`
# below runs AFTER the explicit pin and lets each commit's own metadata win — verified in the
# built images: tokenize-rt 4.2.1 in pr-371, 6.0.0 in pr-885, both on Python 3.8.20.
# One era file is therefore correct here; a split would build two identical images.
#
# The 3.8 ceiling is not incidental: this tool rewrites source via the ast + tokenize modules,
# both of which changed materially after 3.8, so the runtime follows the repo's own pins
# rather than the newest available.
LANG_IMAGE = "python:3.8-slim-bullseye"

APT = ["bash", "ca-certificates", "git"]

# --junitxml    : machine-readable; see parse_junit_log below.
# --override-ini: setup.cfg wires covdefaults/coverage into addopts; neutralise it so a
#                 missing coverage plugin cannot abort a graded stage.
TEST_CMD = (
    "python -m pytest tests/ -v --tb=short "
    "--override-ini=addopts= -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/results.xml"
)

# The `tokenize-rt<5` pin is a FLOOR for the oldest PR in this range, not a ceiling for all
# of them, and the ORDER of these two lines is what makes that work. #371 (2020) needs the
# pre-5 API; #885 (2023) declares `tokenize-rt>=5.2.0` in its own setup.cfg, and because the
# editable install runs second, pip upgrades the pin to satisfy that commit's metadata.
# Verified in the built images: 4.2.1 in pr-371, 6.0.0 in pr-885. Tests use
# `from unittest import mock` (stdlib), so no `mock` package is needed.
PROVISION = """pip install --no-cache-dir "tokenize-rt<5" pytest
pip install --no-cache-dir -e /home/pyupgrade"""

GATE = """python -c "import pyupgrade, tokenize_rt, pytest; print('imports ok')"
cd /home/pyupgrade && python -m pytest tests/ --collect-only -q \\
    --override-ini=addopts= -p no:cacheprovider"""


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
        # bullseye=True: that suite's pool has been pruned upstream — see
        # apt_block above. Python 3.8 has no bookworm variant, so staying on
        # bullseye and dropping the security suite is the only option.
        return base_dockerfile(
            self.pr,
            self.dependency(),
            apt_packages=APT,
            bullseye=True,
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
            File(".", "run_tests.sh", run_tests_sh(self.pr.repo, TEST_CMD)),
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


# Registered under BOTH keys on purpose. Instance.create derives its lookup key
# from pr.number_interval when that field is set, and from {org}/{repo} when it
# is not (instance.py:41-51). The JSONL for these PRs leaves number_interval unset,
# so the live key is "asottile/pyupgrade"; the interval key matches this file's name and
# covers a JSONL that does set it. PRs covered: [371, 885].
@Instance.register("asottile", "pyupgrade_885_to_371")
@Instance.register("asottile", "pyupgrade")
class ASOTTILE_PYUPGRADE(Instance):
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
