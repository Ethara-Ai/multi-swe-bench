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
# kids-first/kf-api-study-creator — a Django + Graphene (GraphQL) service.
#
# Architecture: shared base, ERA-SHARDED (req.txt / QC Reference A) — see the SHARED BUILD BLOCKS section below.
#
# Unlike a library repo this suite needs a live PostgreSQL: every settings module (testing
# included) hard-codes ENGINE=django.db.backends.postgresql and pytest-django creates and
# destroys a real `test_postgres` database per run. The server is installed into the base and
# started by run_tests.sh at the top of EVERY graded stage — each stage is a fresh
# `docker run`, so a server started once at build time would not survive into any of them.

# The repo's own Dockerfile moved from python:3.7-alpine (Django 2.1.11, graphene 2.1.3 —
# PRs 391/392, May 2020) to python:3.8-slim-buster (Django 2.2.24, graphene 2.1.8 — PR 684,
# July 2021). One interpreter cannot serve both eras, so the base image — and therefore the
# shared base TAG — follows the tree. Each era gets its own shared base, which is what the
# `base-<lo>_to_<hi>` tag shape is for.
#
# Debian slim rather than the upstream Alpine: psycopg2-binary, cryptography and the postgres
# server all ship prebuilt for glibc, and Alpine would force a source build of each.
# ERA ANALYSIS — two eras, deliberately in ONE file. The canonical shape (Check 5B) is one
# .py per era, with the registration key equal to the JSONL `number_interval`. That shape is
# unreachable here: `Instance.create` derives its key from `pr.number_interval`
# (instance.py:41-51), every record in this JSONL leaves that field empty, and two era files
# cannot both claim the single fallback key `kids-first/kf-api-study-creator` — the second
# registration would overwrite the first and one era would silently never build.
#
# So the era split lives inside the file instead: `era_for()` selects both the interpreter
# and the base TAG, which is what actually has to differ. The result is identical to the
# canonical shape at the image level — two distinct shared bases, base-0_to_499 and
# base-500_to_99999 — and `era_for()` raises rather than defaulting if a PR ever falls
# outside every declared band, so a routing gap fails loudly instead of silently building
# against the wrong interpreter.
#
# To migrate to the canonical shape later: set `number_interval` on each JSONL record, then
# split this file on the same boundary, keeping these two base tags so the built images stay
# valid.
ERAS = (
    (0, 499, "python:3.7-slim-bullseye"),
    (500, 99999, "python:3.8-slim-bullseye"),
)


def era_for(number: int) -> tuple[int, int, str]:
    for lo, hi, image in ERAS:
        if lo <= number <= hi:
            return lo, hi, image
    raise ValueError(
        f"PR {number} falls outside every declared kf-api-study-creator era"
    )


# postgresql        : the suite's database (see the module docstring).
# redis-server      : `django_rq` is in INSTALLED_APPS and creator/settings/testing.py points
#                     four RQ queues at localhost:6379. Only the `default` queue sets
#                     ASYNC=False; cavatica/dataservice/aws all expect a live broker. Without
#                     it those tests die with "Error 111 connecting to localhost:6379" — and
#                     because hypothesis re-runs a failing example to confirm it, the
#                     inconsistent outcome surfaces as `hypothesis.errors.Flaky`, which is
#                     what made the pass/fail counts wander between runs (16/17/19 baseline
#                     failures on consecutive builds) and cost pr-392 its verdict entirely
#                     under Report.check rule 2.
# libpq-dev + gcc/… : psycopg2 and cryptography fall back to a source build on any arch
#                     without a matching wheel.
APT = [
    "bash",
    "build-essential",
    "ca-certificates",
    "git",
    "libffi-dev",
    "libpq-dev",
    "libssl-dev",
    "postgresql",
    "postgresql-client",
    "redis-server",
]

# --junitxml    : machine-readable; see parse_junit_log below.
# --override-ini: addopts carry `--codestyle` (pytest.ini, 2020) or `--pycodestyle`
#                 (setup.cfg, 2021) plus `--cov`. Style findings are not behaviour: they
#                 would appear as extra "tests" whose pass/fail moves with unrelated
#                 formatting, and a missing plugin would abort the stage outright.
TEST_CMD = (
    "python -m pytest tests/ -v --tb=short "
    "--override-ini=addopts= -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/results.xml"
)

# PG_HOST is the unix SOCKET DIRECTORY, not a TCP address, and that is deliberate.
# Concurrent BuildKit RUN steps — the amd64 and arm64 halves of one multi-platform build,
# and sibling images built in parallel — share a network namespace, so two builds that each
# bind postgres to 127.0.0.1:5432 collide with "could not bind ... Address already in use /
# FATAL: could not create any TCP/IP sockets". It is timing-dependent, so it passes until it
# suddenly doesn't. A unix socket lives in each build's own filesystem layer, so it cannot
# collide. Django/psycopg2 treat a HOST beginning with "/" as a socket directory and use
# PG_PORT to pick the socket file (.s.PGSQL.5432).
PG_ENV = """export DJANGO_SETTINGS_MODULE=creator.settings.testing
export PG_HOST=/var/run/postgresql
export PG_PORT=5432
export PG_USER=postgres
export PG_PASS=postgres
export PG_NAME=postgres"""

# Starts BOTH services the suite needs. Idempotent: called from prepare.sh at build time and
# again from run_tests.sh at the top of each graded stage, because every stage is a fresh
# `docker run` and a server started at build time does not survive into any of them.
START_SERVICES = """#!/bin/bash
set -e

PGVER=$(ls /usr/lib/postgresql | sort -V | tail -n1)
PGSOCK=/var/run/postgresql

# UNIX SOCKET ONLY — listen_addresses is emptied so the server binds no TCP port at all.
# Concurrent BuildKit RUN steps share a network namespace, so a TCP listener on 5432 races
# with every sibling build ("FATAL: could not create any TCP/IP sockets"). The socket lives
# in this build's own filesystem layer and cannot collide. Django reaches it because PG_HOST
# is the socket directory.
#
# `trust` on local connections: we run as root and connect as the postgres role, which the
# Debian default `peer` rule rejects (uid mismatch). The server listens on no network
# interface, so this is not reachable from outside the container.
if [ -f "/etc/postgresql/$PGVER/main/pg_hba.conf" ]; then
  printf 'local all all trust\\n' > "/etc/postgresql/$PGVER/main/pg_hba.conf"
fi

pg_ctlcluster "$PGVER" main start -o "-c listen_addresses=''" || true

for _ in $(seq 1 90); do
  if pg_isready -q -h "$PGSOCK" -p 5432; then
    break
  fi
  sleep 1
done

if ! pg_isready -q -h "$PGSOCK" -p 5432; then
  echo "start_services: postgres unix socket never became ready" >&2
  pg_ctlcluster "$PGVER" main status || true
  tail -40 "/var/log/postgresql/postgresql-$PGVER-main.log" 2>/dev/null || true
  exit 1
fi

# Retried: the server accepts connections a moment before it will service a write.
pg_ready=0
for _ in 1 2 3 4 5; do
  if psql -q -U postgres -h "$PGSOCK" -c "SELECT 1;" > /dev/null 2>&1; then
    pg_ready=1
    break
  fi
  sleep 2
done

if [ "$pg_ready" -ne 1 ]; then
  echo "start_services: postgres up but not accepting queries; last attempt:" >&2
  psql -U postgres -h "$PGSOCK" -c "SELECT 1;" >&2 || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Redis. django_rq targets localhost:6379 for four queues; only `default` is ASYNC=False,
# so the rest need a live broker or their tests raise ConnectionError. Unlike postgres this
# one MUST listen on TCP: the port comes from RQ_QUEUES and redis-py is addressed by
# host/port, so a unix socket is not reachable through that settings path.
#
# The start is tolerated because concurrent BuildKit RUN steps share a network namespace, so
# a sibling image building at the same moment may already hold 6379. That is harmless — the
# PING loop below is the hard gate, and it does not care whose server answers: at BUILD time
# any reachable broker satisfies the collect-only gate, and at RUN time each graded stage is
# its own container with its own namespace, so the server is always this container's.
redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --save '' --appendonly no || true

for _ in $(seq 1 60); do
  if redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null | grep -q PONG; then
    echo "start_services: ready (postgres unix socket, redis 127.0.0.1:6379)"
    exit 0
  fi
  sleep 1
done

echo "start_services: redis never answered PING" >&2
redis-cli -h 127.0.0.1 -p 6379 ping >&2 || true
exit 1
"""

# dev-requirements.txt pins the test stack (pytest, pytest-django, factory-boy, moto,
# hypothesis) alongside docs tooling and two `-e git+https://…` editable checkouts (a sphinx
# theme, and codecov at the 2021 commit). The editables clone into ./src, which pollutes
# collection and adds a network dependency for artefacts no test imports; docs and
# coverage-upload tooling is dead weight for the same reason.
# Cython is constrained below 3 for the BUILD environment, which matters only on arm64.
# The 2021 tree pins `PyYAML==5.4` (via kf_lib_data_ingest), and PyYAML 5.4's setup.py is
# incompatible with Cython >= 3 — it dies with `AttributeError: cython_sources`. On amd64
# pip never notices, because a prebuilt manylinux wheel exists for that version; on arm64
# there is no aarch64 wheel, so pip builds from source and hits it. PIP_CONSTRAINT is used
# rather than a plain `pip install "Cython<3"` because pip resolves build requirements in an
# ISOLATED environment that ignores what is already installed — the constraint file is the
# only thing that reaches into it.
PROVISION = """printf 'Cython<3\\n' > /home/pip-constraint.txt
export PIP_CONSTRAINT=/home/pip-constraint.txt

# requirements.txt pulls three dependencies straight from GitHub over https
# (kf_lib_data_ingest, d3b_utils, kf_utils), so this step depends on a live network for the
# whole of its run. A single refused connection has aborted a build here mid-install
# ("Failed to connect to github.com port 443: Connection refused"). pip's own --retries only
# covers index requests, not the `git clone` it shells out to, so the retry has to wrap the
# whole command. Still fails hard after the last attempt: a half-installed environment must
# not reach the graded stages.
pip_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if pip install --no-cache-dir "$@"; then
      return 0
    fi
    echo "pip install failed (attempt ${attempt}/5); retrying in $((attempt * 10))s" >&2
    sleep $((attempt * 10))
  done
  echo "pip install still failing after 5 attempts: $*" >&2
  return 1
}

pip_retry -r requirements.txt

grep -v -E '^-e |^#|sphinx|doc8|codecov' dev-requirements.txt \\
    > /home/dev-requirements.filtered.txt
pip_retry -r /home/dev-requirements.filtered.txt

bash /home/start_services.sh"""

GATE = """python -c "import django, pytest, psycopg2; print('imports ok')"
cd /home/kf-api-study-creator && python manage.py check
cd /home/kf-api-study-creator && python -m pytest tests/ --collect-only -q \\
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
        return era_for(self.pr.number)[2]

    def image_tag(self) -> str:
        lo, hi, _ = era_for(self.pr.number)
        return f"base-{lo}_to_{hi}"

    def workdir(self) -> str:
        return self.image_tag()

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # bullseye=True: that suite's pool has been pruned upstream — see
        # apt_block above. Moving off bullseye is not an option here; Python 3.7 has
        # no bookworm variant.
        # pip is pinned BELOW 24.1, deliberately. requirements.txt at the 2021 commit pins
        # `slack_sdk==3.4.0`, whose wheel ships malformed metadata —
        # `websocket-client (>=0.57<1)`, missing a comma. pip 24.1 tightened metadata
        # validation and now refuses such a release outright ("Please use pip<24.1 if you
        # need to use this version"), so an unpinned `--upgrade pip` silently makes an
        # era-correct requirements file uninstallable. pip 23.x is also simply what these
        # 2020/2021 trees were resolved with.
        return base_dockerfile(
            self.pr,
            self.dependency(),
            apt_packages=APT,
            bullseye=True,
            extra_run=(
                'RUN pip install --no-cache-dir --upgrade "pip<24.1" setuptools wheel'
            ),
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
            File(".", "start_services.sh", START_SERVICES),
            File(
                ".",
                "run_tests.sh",
                run_tests_sh(
                    self.pr.repo,
                    TEST_CMD,
                    env=PG_ENV,
                    prefix="bash /home/start_services.sh",
                ),
            ),
            File(
                ".",
                "prepare.sh",
                prepare_sh(
                    self.pr.repo,
                    self.pr.base.sha,
                    provision=PROVISION,
                    gate=GATE,
                    env=PG_ENV,
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
# so the live key is "kids-first/kf-api-study-creator"; the interval key matches this file's name and
# covers a JSONL that does set it. PRs covered: [391, 392, 684].
@Instance.register("kids-first", "kf_api_study_creator_684_to_391")
@Instance.register("kids-first", "kf-api-study-creator")
class KIDS_FIRST_KF_API_STUDY_CREATOR(Instance):
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
