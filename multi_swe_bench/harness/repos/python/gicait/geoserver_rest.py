"""gicait/geoserver-rest -- Python client for the GeoServer REST API.

Why this repo is unusual
    geoserver-rest is a CLIENT library. Its tests do not exercise pure Python --
    every one of them sends HTTP requests to a live GeoServer and checks the
    replies (tests/common.py):

        GEO_URL = os.getenv("GEO_URL", "http://localhost:8080/geoserver")
        geo = Geoserver(GEO_URL, username=..., password=...)

    The project's own CI starts two services before running pytest
    (.github/workflows/python-test.yml):

        docker compose -f tests/docker-compose.yaml up -d
        sleep 60   # Geoserver takes quite a long time to boot up

    i.e. GeoServer on :8080 and PostGIS on :5432. The harness gives us a single
    container per stage with no docker-compose and no docker-in-docker, so both
    services are installed INTO this image and started by run-tests.sh before
    pytest, once per stage.

Environment, derived from the repo rather than guessed
    python 3.10          one of the versions in the CI matrix; also the newest
                         Debian release that still ships openjdk-17
    openjdk 17           GeoServer 2.24.x supports Java 11/17, not 21. Note
                         python:3.10 (unsuffixed) is now Debian trixie, which
                         only has Java 21 -- hence the explicit -bookworm tag.
    GeoServer 2.24.2     platform-independent binary (embedded Jetty), so no
                         Tomcat is needed
    postgresql 15 + postgis 3.3

GDAL is deliberately NOT installed. requirements_dev.txt pins gdal>=3.4.1, but
nothing the tests import needs it: only geo/Calculation_gdal.py imports osgeo,
and neither tests/test_geoserver.py nor geo/Geoserver.py nor geo/Style.py pulls
that module in. Installing it would mean matching a system GDAL to the Python
binding exactly -- a large, fragile dependency for zero benefit here. The other
requirements_dev entries that ARE reachable (pytest, ddt, environs, sqlalchemy,
psycopg2, seaborn, xmltodict) are installed explicitly.

Single-arch for now. The GeoServer and JDK installs are arch-neutral and the
fixture download is a plain file, so a later multi-arch build needs no change.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Pinned so a rebuild is reproducible. GEOSERVER_VERSION must stay compatible
# with the JDK installed below (2.24.x => Java 11 or 17).
GEOSERVER_VERSION = "2.24.2"

# The PR's test fixture arrives as a BINARY diff section, which `git apply`
# cannot apply and _sanitize_patch therefore strips. The tests need the real
# file on disk, so it is fetched separately, pinned to this PR's head commit.
FIXTURE_SHA = "8092c55460655bdf970e051c74dd5b1362371fa3"
FIXTURE_PATH = "tests/data/tos_O1_2001-2002.nc"


def _sanitize_patch(patch_content: str) -> str:
    """Drop diff sections that cannot change a test outcome but do break `git apply`.

    `git apply` is atomic -- one unusable section rejects the whole patch and the
    stage then produces no results at all. Two kinds are always unusable:

    1. Binary hunks. GitHub renders these as `Binary files a/x and b/y differ`
       with no payload, so grepping only for `GIT binary patch` misses them.
       This PR has exactly one: tests/data/tos_O1_2001-2002.nc, which is why
       prepare.sh downloads that file directly.
    2. Committed build output under `*/build/*`, which does not exist at the
       base commit so the section can never apply.
    """
    if not patch_content:
        return patch_content

    diff_header = re.compile(r"^diff --git a/(.+?) b/(.+)$")
    build_output = re.compile(r"(^|/)build/")

    lines = patch_content.split("\n")
    result = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git"):
            header = diff_header.match(lines[i])
            section_start = i
            i += 1
            is_binary = False
            while i < len(lines) and not lines[i].startswith("diff --git"):
                if lines[i].startswith("GIT binary patch") or lines[i].startswith(
                    "Binary files"
                ):
                    is_binary = True
                i += 1
            is_build_output = bool(header) and bool(
                build_output.search(header.group(1))
                or build_output.search(header.group(2))
            )
            if not (is_binary or is_build_output):
                result.extend(lines[section_start:i])
        else:
            result.append(lines[i])
            i += 1
    return "\n".join(result)


class GeoserverRestImageBase(Image):
    """Base image: python 3.10 + Java 17 + GeoServer + PostGIS, repo at BASE_COMMIT.

    The hardening block lives in THIS layer, which is safe because image_tag()
    below is per-PR (base-pr-<N>) rather than the shared "base" most configs
    use. Each PR therefore gets its own base image built with its own
    BASE_COMMIT, so the block's `gc --prune` can only ever prune history that
    is irrelevant to that PR. With a SHARED base tag this would be wrong: the
    image would be built once with whichever BASE_COMMIT arrived first, and the
    prune would delete every other PR's base commit.
    """

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
        # a str dependency is also what makes build_dataset.py pass REPO_URL and
        # BASE_COMMIT through as build args (build_dataset.py: isinstance(dep, str))
        return "python:3.10-bookworm"

    def image_tag(self) -> str:
        # Per-PR tag (base-pr-<N>) rather than the shared "base" most configs
        # use. Two reasons: it matches the reference base Dockerfile the QC
        # checklist is written against, and it makes the hardening block in
        # this layer correct by construction -- each PR gets its own base image
        # built with its own BASE_COMMIT, so `gc --prune` can never delete
        # another PR's base commit.
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        org, repo = self.pr.org, self.pr.repo

        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

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

{self.global_env}

# The tests read every connection detail from the environment (tests/common.py),
# so pointing them at the services inside this container needs no source edit --
# which matters, because a modified work tree would break `git apply`.
# JAVA_HOME is deliberately NOT set here.
#
# The obvious form, /usr/lib/jvm/java-17-openjdk-${{TARGETARCH}}, is a trap: the
# harness builds with the classic builder, where BuildKit does not populate
# TARGETARCH, so it expands to EMPTY and JAVA_HOME becomes the non-existent
# /usr/lib/jvm/java-17-openjdk- . GeoServer's startup.sh prefers
# "$JAVA_HOME/bin/java" whenever JAVA_HOME is set, so a wrong value is worse
# than none -- it would fail to start with a confusing "no java" error.
# Leaving it unset makes startup.sh fall back to java on PATH, which the Debian
# package installs at /usr/bin/java. start-services.sh sets a correct JAVA_HOME
# at runtime anyway, derived from the real java binary.
ENV GEOSERVER_HOME=/opt/geoserver \\
    GEO_URL=http://localhost:8080/geoserver \\
    GEO_USER=admin \\
    GEO_PASS=geoserver \\
    DB_HOST=localhost \\
    DB_PORT=5432 \\
    DB_NAME=geodb \\
    DB_USER=geodb_user \\
    DB_PASS=geodb_pass \\
    PGDATA=/var/lib/postgresql/data \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONUNBUFFERED=1

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    ca-certificates curl git gnupg unzip xz-utils procps netcat-openbsd \\
    openjdk-17-jre-headless \\
    postgresql postgresql-contrib postgresql-15-postgis-3 \\
    && rm -rf /var/lib/apt/lists/* \\
    && java -version

# GeoServer platform-independent binary: it embeds Jetty, so no Tomcat is
# needed.
#
# Source order matters here. Measured from inside this image:
#   build.geoserver.org        3345 KB/s   (~2 min for the archive)
#   downloads.sourceforge.net   249 KB/s   (~27 min)
# so the project's own build host is tried first and SourceForge is the
# fallback. The SourceForge /projects/.../download URL shape is deliberately
# avoided -- it returns a ~48 KB HTML interstitial rather than the zip.
#
# Trade-off worth knowing: the fast URL is the 2.24.x "latest" build rather than
# a pinned patch release, so a rebuild months from now could pick up a later
# 2.24.x. That does not affect this PR's outcome (the tests exercise the REST
# API, not GeoServer internals), and the fallback IS pinned to {GEOSERVER_VERSION}.
#
# The extraction handles both layouts: the SourceForge zip contains a single
# geoserver-<ver>/ directory, the build host's zip may unpack at the root.
RUN (curl -fsSL --retry 4 --retry-delay 5 --retry-all-errors --max-time 900 \\
        -o /tmp/geoserver.zip \\
        "https://build.geoserver.org/geoserver/2.24.x/geoserver-2.24.x-latest-bin.zip" \\
     || curl -fsSL --retry 8 --retry-delay 5 --retry-all-errors --max-time 1800 \\
        -o /tmp/geoserver.zip \\
        "https://downloads.sourceforge.net/project/geoserver/GeoServer/{GEOSERVER_VERSION}/geoserver-{GEOSERVER_VERSION}-bin.zip") \\
    && mkdir -p /opt/geoserver /tmp/gs \\
    && unzip -q /tmp/geoserver.zip -d /tmp/gs \\
    && if [ "$(find /tmp/gs -maxdepth 1 -mindepth 1 | wc -l)" = "1" ] && [ -d "$(find /tmp/gs -maxdepth 1 -mindepth 1 -type d)" ]; then \\
           mv "$(find /tmp/gs -maxdepth 1 -mindepth 1 -type d)"/* /opt/geoserver/ ; \\
       else \\
           mv /tmp/gs/* /opt/geoserver/ ; \\
       fi \\
    && rm -rf /tmp/geoserver.zip /tmp/gs \\
    && chmod +x /opt/geoserver/bin/*.sh \\
    && test -f /opt/geoserver/start.jar \\
    && echo "geoserver installed:" && ls /opt/geoserver | head -10

# NetCDF extension -- REQUIRED, not optional.
#
# PR 178 adds coveragestore support for NetCDF, and its tests upload a .nc file
# with file_type="NetCDF". Vanilla GeoServer cannot read NetCDF at all: it is a
# separate downloadable extension. Without it GeoServer answers the upload with
#     GeoserverException: Status : 400 - b''
# followed by
#     404 - No such coverage store: test_workspace,netcdf
# and BOTH new tests fail even with the fix patch applied -- which would make a
# perfectly good PR look unresolvable. The project's CI never hits this because
# it uses the kartoza/geoserver image, which bundles the extensions.
#
# The plugin version must match the GeoServer build exactly or the jars clash,
# so the version is read back from GeoServer's own gs-main-<ver>.jar rather than
# hardcoded -- that keeps it correct whichever of the two sources above served
# the archive.
# Verified in-container: with these jars present, both new tests go from FAILED
# to PASSED (2 passed, 1 skipped).
RUN LIB=/opt/geoserver/webapps/geoserver/WEB-INF/lib \\
    && GSVER="$(ls $LIB/gs-main-*.jar | head -1 | sed 's|.*/gs-main-||; s|\\.jar$||')" \\
    && echo "detected geoserver version: $GSVER" \\
    && (curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors --max-time 900 \\
            -o /tmp/netcdf.zip \\
            "https://build.geoserver.org/geoserver/2.24.x/ext-latest/geoserver-$GSVER-netcdf-plugin.zip" \\
        || curl -fsSL --retry 8 --retry-delay 5 --retry-all-errors --max-time 1800 \\
            -o /tmp/netcdf.zip \\
            "https://downloads.sourceforge.net/project/geoserver/GeoServer/{GEOSERVER_VERSION}/extensions/geoserver-{GEOSERVER_VERSION}-netcdf-plugin.zip") \\
    && unzip -o -q /tmp/netcdf.zip -d "$LIB" \\
    && rm /tmp/netcdf.zip \\
    && test "$(ls $LIB | grep -ic netcdf)" -ge 2 \\
    && echo "netcdf extension installed:" && ls $LIB | grep -i netcdf

# One-off PostgreSQL cluster owned by root's postgres user, with the database,
# role and PostGIS extension the tests expect (tests/common.py defaults).
RUN mkdir -p /var/lib/postgresql/data /var/run/postgresql \\
    && chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql \\
    && su postgres -c "/usr/lib/postgresql/15/bin/initdb -D /var/lib/postgresql/data" \\
    && su postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/postgresql/data -w start" \\
    && su postgres -c "psql -c \\"CREATE USER geodb_user WITH SUPERUSER PASSWORD 'geodb_pass';\\"" \\
    && su postgres -c "psql -c \\"CREATE DATABASE geodb OWNER geodb_user;\\"" \\
    && su postgres -c "psql -d geodb -c 'CREATE EXTENSION IF NOT EXISTS postgis;'" \\
    && su postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/postgresql/data -w stop"

RUN git clone "${{REPO_URL}}" /home/{repo}

WORKDIR /home/{repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}
{self.clear_env}

CMD ["/bin/bash"]
"""


class GeoserverRestImageDefault(Image):
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
        return GeoserverRestImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        filtered_fix_patch = _sanitize_patch(self.pr.fix_patch)
        filtered_test_patch = _sanitize_patch(self.pr.test_patch)

        return [
            File(".", "fix.patch", f"{filtered_fix_patch}"),
            File(".", "test.patch", f"{filtered_test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """#!/bin/bash
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

""",
            ),
            File(
                ".",
                "start-services.sh",
                """#!/bin/bash
# Bring up the two services the tests talk to, and do not return until both
# actually answer. Sourced by run-tests.sh at the start of EVERY stage, because
# each stage runs in a fresh container.
#
# Deliberately no `set -e`: a service that fails to start must be reported by
# the readiness probes below (which produce a clear message), not by an opaque
# early exit.

# Derive JAVA_HOME from the real java binary rather than trusting an inherited
# value. GeoServer's startup.sh prefers "$JAVA_HOME/bin/java" whenever JAVA_HOME
# is non-empty, so a stale or wrong value stops it starting with a misleading
# error. readlink -f resolves the /usr/bin/java alternatives symlink down to
# .../jvm/java-17-openjdk-<arch>/bin/java, and two dirnames give the home.
JAVA_BIN="$(readlink -f "$(command -v java)" 2>/dev/null)"
if [ -n "$JAVA_BIN" ]; then
  export JAVA_HOME="$(dirname "$(dirname "$JAVA_BIN")")"
else
  unset JAVA_HOME
fi
echo "services: JAVA_HOME=${JAVA_HOME:-<unset, using PATH>}"
java -version 2>&1 | head -1 | sed 's/^/services: /'

echo "services: starting postgresql"
su postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/postgresql/data -l /tmp/pg.log -w start" || true

pg_ready=0
for i in $(seq 1 30); do
  if pg_isready -h localhost -p 5432 > /dev/null 2>&1; then
    pg_ready=1
    echo "services: postgresql ready"
    break
  fi
  sleep 2
done
if [ "$pg_ready" -ne 1 ]; then
  echo "services: WARNING postgresql did not become ready; postgis-backed tests will fail"
  tail -20 /tmp/pg.log 2>/dev/null
fi

echo "services: starting geoserver (this takes around a minute)"
cd /opt/geoserver
nohup ./bin/startup.sh > /tmp/geoserver.log 2>&1 &
cd - > /dev/null

# Poll the REST API rather than sleeping a fixed 60s like the project's CI --
# the container may be slower or faster, and a fixed sleep is either wasteful or
# flaky. about/version.json is the cheapest authenticated endpoint.
geo_ready=0
for i in $(seq 1 90); do
  if curl -sf -u admin:geoserver http://localhost:8080/geoserver/rest/about/version.json > /dev/null 2>&1; then
    geo_ready=1
    echo "services: geoserver ready after approximately $((i * 2))s"
    break
  fi
  sleep 2
done
if [ "$geo_ready" -ne 1 ]; then
  echo "services: FATAL geoserver never became ready after 180s"
  tail -40 /tmp/geoserver.log 2>/dev/null
fi

""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
# Defensive base-commit recovery. `git cat-file -e` short-circuits when the
# object is already present, so these fetches normally cost nothing; they matter
# only if the branch carrying this base commit is deleted upstream. The
# temporary ref is deleted afterwards so the hardening block's
# `rev-list --all == rev-list HEAD` assertion still holds.
git cat-file -e {pr.base.sha} 2>/dev/null \\
    || git fetch --quiet --no-tags origin "{pr.base.sha}:refs/mswb/base" \\
    || git fetch --quiet origin "+refs/pull/*/head:refs/mswb/pull/*" \\
    || true
git checkout {pr.base.sha}
git for-each-ref --format='%(refname)' refs/mswb | xargs -r -n1 git update-ref -d
bash /home/check_git_changes.sh

# ---------------------------------------------------------------------------
# Everything below runs at BUILD time, where the network is available and the
# output is not parsed.
#
# NOTE none of these steps end in `|| true`. On an earlier repo that idiom
# swallowed download failures and produced images that reported "built
# successfully" yet could not run a single test. Here a failure fails the build.
# ---------------------------------------------------------------------------

python -m pip install --upgrade pip wheel

# Installed explicitly rather than via `pip install -r requirements_dev.txt`,
# because that file pins gdal>=3.4.1 and nothing in the tests' import chain uses
# GDAL (only geo/Calculation_gdal.py imports osgeo, and no test imports it).
# Pulling GDAL in would mean matching a system library to the Python binding
# exactly, for no benefit. Everything below IS reachable from the tests:
#   requests, xmltodict  <- geo/Geoserver.py
#   seaborn, matplotlib  <- geo/Style.py
#   pytest, sqlalchemy   <- tests/test_geoserver.py
#   psycopg2             <- the postgis-backed tests
#   ddt, environs        <- requirements_dev.txt, imported by other test modules
pip_ok=0
for attempt in 1 2 3 4 5; do
  if pip install \\
      pytest \\
      ddt \\
      environs \\
      "sqlalchemy>=2.0.29" \\
      psycopg2-binary \\
      "xmltodict>=0.13.0" \\
      "seaborn>=0.13.2" \\
      pygments \\
      requests; then
    pip_ok=1
    break
  fi
  sleep_for=$(( attempt * 20 ))
  echo "prepare: pip install attempt $attempt failed; sleeping $sleep_for s"
  sleep $sleep_for
done
if [ "$pip_ok" -ne 1 ]; then
  echo "prepare: FATAL - could not install python dependencies"
  exit 1
fi

# install the package under test itself, so `import geo` resolves
pip install -e . || pip install .

# ---------------------------------------------------------------------------
# The PR's binary test fixture.
#
# The test patch adds tests/data/tos_O1_2001-2002.nc as a BINARY diff section.
# `git apply` cannot apply binary hunks, so _sanitize_patch strips it -- but the
# new tests reference it directly:
#     self.path = f"HERE/data/tos_O1_2001-2002.nc"
# Without the file both testable new tests fail with FileNotFoundError, for a
# reason unrelated to the fix. So fetch it from the PR's own head commit.
#
# It is registered in .git/info/exclude rather than .gitignore: that file is
# local-only and untracked, so the work tree stays clean and both
# check_git_changes.sh and the later `git apply` remain trustworthy.
# ---------------------------------------------------------------------------
mkdir -p tests/data
if ! curl -fsSL --retry 8 --retry-delay 5 --retry-all-errors --max-time 600 \\
        -o {fixture_path} \\
        "https://raw.githubusercontent.com/{pr.org}/{pr.repo}/{fixture_sha}/{fixture_path}"; then
  echo "prepare: FATAL - could not download the test fixture {fixture_path}"
  exit 1
fi
grep -qxF '{fixture_path}' .git/info/exclude 2>/dev/null || echo '{fixture_path}' >> .git/info/exclude
ls -la {fixture_path}

# ---------------------------------------------------------------------------
# Prove the services actually come up at BUILD time. If GeoServer cannot start
# here it will not start during a graded stage either, and an image that looks
# healthy but cannot serve a single request is worse than one that fails now.
# ---------------------------------------------------------------------------
bash /home/start-services.sh
if ! curl -sf -u admin:geoserver http://localhost:8080/geoserver/rest/about/version.json > /dev/null; then
  echo "prepare: FATAL - geoserver did not answer during the build check"
  exit 1
fi
echo "prepare: geoserver verified at build time"
pg_isready -h localhost -p 5432 || echo "prepare: WARNING postgres not ready (postgis tests will fail consistently)"

# leave nothing running; every stage starts its services itself
su postgres -c "/usr/lib/postgresql/15/bin/pg_ctl -D /var/lib/postgresql/data -w stop" || true
/opt/geoserver/bin/shutdown.sh > /dev/null 2>&1 || true
sleep 3

# The three stages must each start from an identical pristine tree, or
# `git apply` cannot be trusted.
git reset --hard
bash /home/check_git_changes.sh

""".format(
                    pr=self.pr,
                    fixture_path=FIXTURE_PATH,
                    fixture_sha=FIXTURE_SHA,
                ),
            ),
            File(
                ".",
                "run-tests.sh",
                """#!/bin/bash
# Deliberately NO `set -e`: the test stage is EXPECTED to fail, and the suite
# must run to completion in every stage or the test name sets stop matching
# across stages. The exit status is preserved and re-raised at the end.
rc=0

bash /home/start-services.sh

cd /home/{pr.repo}

# -v gives the "path::testname STATUS" lines parse_log matches.
# -p no:cacheprovider keeps pytest from writing .pytest_cache into the work
# tree, which would dirty it for the next stage.
python -m pytest tests/ -v -p no:cacheprovider --tb=short || rc=$?

exit $rc

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

bash /home/run-tests.sh

""",
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/run-tests.sh

""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        repo = self.pr.repo

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

WORKDIR /home/{repo}

{self.clear_env}

"""


@Instance.register("gicait", "geoserver-rest")
class GEOSERVER_REST(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return GeoserverRestImageDefault(self.pr, self._config)

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
        """Parse pytest output.

        Two line shapes are handled, because pytest switches format depending on
        terminal width and on whether -v is in effect:

            tests/test_geoserver.py::TestCoveragestore::test_x PASSED   [ 12%]
            FAILED tests/test_geoserver.py::TestCoveragestore::test_y - AssertionError

        ERROR folds into failed (a fixture or collection error means the test did
        not pass) and XFAIL/XPASS into skipped, so a test can never land in two
        sets at once.
        """
        passed_tests = set()
        failed_tests = set()
        skipped_tests = set()

        # ANSI first: pytest colourises status words, and an invisible escape
        # before the word stops every pattern from matching.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        verbose_re = re.compile(
            r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
        )
        summary_re = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+::\S+)"
        )

        for line in clean_log.splitlines():
            line = line.rstrip()

            name = status = None
            m = verbose_re.match(line)
            if m:
                name, status = m.group(1), m.group(2)
            else:
                m = summary_re.match(line)
                if m:
                    status, name = m.group(1), m.group(2)

            if not name:
                continue

            if status == "PASSED":
                passed_tests.add(name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(name)
            else:
                skipped_tests.add(name)

        # keep the sets disjoint so the harness cannot double-count a test that
        # appears both in the progress lines and the failure summary
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
