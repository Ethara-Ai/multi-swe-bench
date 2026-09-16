from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

SYNTAX_DIRECTIVE = "# syntax=docker/dockerfile:1.6"
BEGIN_MARKER = "===== BEGIN TEST DETAIL ====="
END_MARKER = "===== END TEST DETAIL ====="


def _arg_env_label(org: str, repo: str) -> str:
    return f'''ARG TARGETARCH
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
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt'''


def apt_block(packages: list[str], *, bullseye: bool = False) -> str:
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
    sections.append("RUN git config --global --add safe.directory '*'")
    sections.append("WORKDIR /home/")
    sections.append(
        f'RUN git clone "${{REPO_URL}}" /home/{pr.repo} \\\n'
        f"    && cd /home/{pr.repo} \\\n"
        "    && git rev-parse HEAD >/dev/null"
    )
    sections.append('CMD ["/bin/bash"]')
    return "\n\n".join(sections) + "\n"


def _prune_block(repo: str, sha: str) -> str:
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
    copy_commands = "".join(f"COPY {f.name} /home/\n" for f in files)
    return f"""FROM {base_full_name}

{copy_commands}
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

TEST_CMD = (
    "python -m pytest tests/ -v --tb=short "
    "--override-ini=addopts= -p no:cacheprovider "
    "--continue-on-collection-errors "
    "--junitxml=/home/results.xml"
)

PG_ENV = """export DJANGO_SETTINGS_MODULE=creator.settings.testing
export PG_HOST=/var/run/postgresql
export PG_PORT=5432
export PG_USER=postgres
export PG_PASS=postgres
export PG_NAME=postgres"""

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

        text = _ANSI_RE.sub("", test_log)
        if BEGIN_MARKER not in text or END_MARKER not in text:
            return TestResult(0, 0, 0, set(), set(), set())
        xml = text.split(BEGIN_MARKER, 1)[1].split(END_MARKER, 1)[0].strip()
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return TestResult(0, 0, 0, set(), set(), set())

        for tc in root.iter("testcase"):
            path = tc.get("file")
            if not path:
                classname = tc.get("classname") or ""
                path = classname.replace(".", "/") + ".py"
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
