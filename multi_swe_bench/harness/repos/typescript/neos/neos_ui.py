"""Repo config for neos/neos-ui (Neos CMS backend UI -- TypeScript/React + PHP).

Runner
------
The gold test is a **TestCafe browser end-to-end test**, not a unit test:
``Tests/IntegrationTests/Fixtures/1Dimension/issue-3184.e2e.js`` drags document
nodes in the Neos backend, discards the change, and asserts no error flash
message appears. The repo's own ``yarn test`` (Jest, ``packages/*/src/**/*.spec.*``)
never collects it.

Running it therefore requires a whole application, not a test runner:

* **PHP 8.2 + Composer + the Flow framework CLI** -- the Neos backend
* **MariaDB** -- ``site:import`` writes the content repository
* **A web server** -- ``./flow server:run --port 8081``; ``utils.js`` hardcodes
  ``http://127.0.0.1:8081/neos``
* **Chromium** -- driven by TestCafe 2.1
* **Node 20 + Yarn 3.2** -- to build the UI bundle the browser loads

Upstream splits this across two containers (``Tests/IntegrationTests/docker-compose.yaml``:
a ``php`` service and a ``mysql:8`` service) with TestCafe on the host. The
harness runs one container per stage, so all of it is collapsed into a single
image here. That is why the DB host needs no rewrite: ``Settings.yaml`` already
says ``127.0.0.1``, and upstream only rewrites it to ``db`` because of the
compose split.

Layout
------
The repo is not the application -- it is a *package inside* one. The image lays
out::

    /opt/neos-e2e/                 <- contents of Tests/IntegrationTests
    /opt/neos-e2e/TestDistribution/Packages/Application/Neos.Neos.Ui   <- the repo

``Tests/IntegrationTests/TestDistribution`` lives inside the repo, so copying the
repo into it would recurse (``cp: cannot copy a directory into itself``).
Upstream's ``e2e-docker.sh`` avoids that by copying ``Tests/IntegrationTests`` to
a separate directory first; this config does the same.

Test identity
-------------
Reported as ``<spec file>::<test name>``, with the path made repo-relative::

    Tests/IntegrationTests/Fixtures/1Dimension/issue-3184.e2e.js::Scenario #1: ...

TestCafe's ``--reporter json`` emits ``fixtures[].path`` (absolute) and
``tests[].name``, so both halves come straight from the reporter -- no marker
lines or filesystem lookups needed, unlike the Mocha and Gradle configs.

Dependency direction
--------------------
The gold test patch *adds* composer requirements to
``TestDistribution/composer.json``: ``neos/neos-development-collection`` and
``cweagans/composer-patches``. The run stages are offline, so the image installs
the **union** of the base and test-patch requirements at build time and then
restores the pristine tree.

That is not merely an optimisation. With the base ``composer.json`` alone,
composer resolves an inconsistent set (``neos/media 8.3.6`` against a much newer
UI) and ``site:import`` dies with::

    Could not convert target type "Neos\\Media\\Domain\\Model\\ImageVariant" ...
    at property path "aspectRatio": Could not find a suitable type converter for "mixed"

because ``CropImageAdjustment::setAspectRatio()`` is documented
``@param AspectRatio | string | null`` and Flow's PropertyMapper cannot resolve
that union. Adding the development collection -- exactly what the test patch
does -- aligns the versions and the import succeeds. The whole environment
depends on it, so it is installed for every stage.

Note the consequence for the RUN stage: its *code* is unpatched, but its
*dependency set* is the union. Dependencies are environment rather than code
under test, so this is the same trade-off every offline config makes, just
larger.

Toolchain pin
-------------
``php:8.2-cli`` -- the official image, deliberately *not* the
``thecodingmachine/php`` one upstream's compose uses. That image defaults to a
non-root ``USER``, and ``DockerfileEnhancer`` injects its CA-symlink block
immediately after ``FROM``, before any ``USER root`` in this file can apply, so
the base image fails to build with permission denied on ``/etc/pki``. Running as
root also removes the ``sudo`` dependency from every script here.

Where the toolchain is installed
-------------------------------
The base Dockerfile keeps the canonical shape -- FROM, ARGs, ENV, LABEL,
CA symlinks, clone, scrub, CMD -- and carries **one** apt line, for ``git``.
That line cannot be dropped: the base's own ``git clone "${REPO_URL}"`` needs
git, and ``php:8.2-cli`` does not ship it. (The reference base needs no apt
block at all only because the ``node:*`` images do ship git.)

Everything else is installed by ``prepare.sh``. That is still **image-build
time** -- it runs as ``RUN bash /home/prepare.sh`` in the PR image, as root and
with network -- so nothing has moved into the graded stages and the run stages
still need no network (verified: the baseline stage scores an identical
34/0/0 under ``docker run --network none``). What ``prepare.sh`` adds:

* **PHP extensions** -- ``gd`` (``Settings.yaml`` sets ``Neos.Imagine.driver: Gd``)
  and ``pdo_mysql``. ``composer check-platform-reqs`` asks for nothing else that
  php:8.2-cli does not already ship.
* **MariaDB** rather than ``mysql:8`` -- Debian bookworm has no ``mysql-server``
  package. Flow connects over TCP as ``root@127.0.0.1``, but MariaDB's packaged
  root uses ``unix_socket`` auth and has no TCP grant, so both are created
  explicitly.
* **Node 20** (``.nvmrc`` says 20.9) and **Chromium**, which the compose split
  provided as separate services.
* **rsync**, used by the run scripts to mirror the patched clone into the
  distribution's package slot.

Architecture
------------
**Multi-arch capable (linux/amd64 + linux/arm64).** The browser is Debian's
``chromium``, not Google's ``google-chrome-stable``: Google publishes that
package for amd64 only, which would confine the instance to one architecture.
Debian builds chromium for arm64 too, at the same version, and TestCafe
recognises ``chromium`` as a browser alias. Everything else in the stack --
PHP, MariaDB, Node, the composer and yarn trees -- is already arch-neutral, so
no architecture guard is needed.

The trade-off: the shared toolchain no longer sits in a cacheable base layer, so
every PR of this repo re-installs it. Base build drops to roughly two minutes
and the PR image absorbs the rest.
"""

import json
import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest
from multi_swe_bench.harness.test_result import get_modified_files

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)

APP_DIR = "/opt/neos-e2e"
DIST_DIR = f"{APP_DIR}/TestDistribution"
UI_DIR = f"{DIST_DIR}/Packages/Application/Neos.Neos.Ui"
E2E_FIXTURE = "1Dimension"


def _gold_test_exclude_flags(test_patch: str) -> str:
    """``git apply --exclude`` flags for every file the gold test patch touches.

    Reward-hacking guard, defence in depth for
    ``test_result.fix_patch_tampers_with_tests``: that pre-run check reads
    ``get_modified_files``, which drops entries whose ``---`` side is
    ``/dev/null`` and is therefore blind to gold tests the test patch
    *creates* -- which is exactly what this PR does.
    """
    text = (test_patch or "").replace("\r\n", "\n").replace("\r", "\n")
    paths = {m.group(2) for m in _DIFF_GIT_RE.finditer(text)}
    paths |= set(get_modified_files(test_patch or ""))
    return " ".join(f"--exclude={shlex.quote(p)}" for p in sorted(paths))


_RUN_E2E = f"""export NODE_OPTIONS="--dns-result-order=ipv4first"

mkdir -p /var/run/mysqld /var/lib/mysql && chown -R mysql:mysql /var/run/mysqld /var/lib/mysql
mariadbd-safe --user=mysql > /tmp/mariadb.log 2>&1 &
for i in $(seq 1 90); do
    mariadb-admin ping --silent 2>/dev/null && break
    sleep 1
done

cd {DIST_DIR}
./flow server:run --port 8081 --host 0.0.0.0 > /tmp/flow-server.log 2>&1 &
for i in $(seq 1 90); do
    curl -sf -o /dev/null http://127.0.0.1:8081/neos && break
    sleep 2
done
printf '##### MSWEB-SERVER-STATUS: %s\\n' "$(curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:8081/neos)"

cd {UI_DIR}
e2e_status=0
timeout -k 60 3600 ./node_modules/.bin/testcafe \\
    "chromium:headless --no-sandbox --disable-dev-shm-usage --disable-gpu" \\
    "Tests/IntegrationTests/Fixtures/{E2E_FIXTURE}/*.e2e.js" \\
    --reporter json:/tmp/testcafe.json --selector-timeout=10000 --assertion-timeout=30000 \\
    > /tmp/testcafe.out 2>/tmp/testcafe.err || e2e_status=$?
printf '##### MSWEB-E2E-EXIT: %s\\n' "$e2e_status"

echo '##### MSWEB-TESTCAFE-JSON'
cat /tmp/testcafe.json
echo
echo '##### MSWEB-TESTCAFE-STDERR'
tail -40 /tmp/testcafe.err"""

_APPLY_EXCLUDES = "--exclude yarn.lock --exclude composer.lock"


class NeosUiImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- the whole Neos application stack.

    Tagged per PR rather than with a bare ``:base``: a single shared tag would
    be rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.
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

    def dependency(self) -> str | Image:
        return "php:8.2-cli"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f"RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}"
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return f"""FROM {image_name}

{self.global_env}

RUN apt-get update && apt-get install -y --no-install-recommends \\
        git ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/

{code}

{self.clear_env}

"""


class NeosUiImageDefault(Image):
    """Per-PR image -- assembles the distribution, builds the UI, seeds the DB."""

    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Image | None:
        return NeosUiImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        gold_excludes = _gold_test_exclude_flags(self.pr.test_patch)

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
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
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
# `-e` is load-bearing, not decoration. Without it the `check_git_changes.sh`
# calls below are advisory: a dirty tree or a failed checkout prints its
# complaint, the script runs on, and the image builds green -- an assertion
# that cannot fail the build is not an assertion. The commands that are
# *allowed* to fail carry their own `|| true`.
set -euxo pipefail

export DEBIAN_FRONTEND=noninteractive

# Everything the e2e stack needs beyond PHP itself is installed here rather than
# in the base Dockerfile, so that file keeps the canonical shape: FROM, ARGs,
# ENV, LABEL, CA symlinks, clone, scrub, CMD. The base carries only `git`,
# without which its own `git clone` cannot run -- php:8.2-cli does not ship it,
# unlike the node:* images where the reference base needs no apt block at all.

# Debian's `chromium`, not Google's `google-chrome-stable`. Google publishes
# that package for amd64 only, which would make this instance amd64-only; Debian
# builds chromium for arm64 as well, at the same version (151.0.7922.137 on both
# at time of writing), so the image can go multi-arch. TestCafe recognises
# `chromium` as a browser alias and finds /usr/bin/chromium without extra config.
apt-get update
apt-get install -y --no-install-recommends \
    curl gnupg unzip rsync procps \
    chromium \
    mariadb-server \
    libpng-dev libjpeg62-turbo-dev libfreetype6-dev libzip-dev libxml2-dev

# `composer check-platform-reqs` on the assembled distribution asks for ctype,
# dom, filter, hash, json, libxml, mbstring, openssl, pcre, pdo, phar,
# reflection, spl, tokenizer, xml, xmlreader, xmlwriter and zlib -- all already
# in php:8.2-cli. Only pdo_mysql (the content repository) and gd
# (Settings.yaml sets `Neos.Imagine.driver: Gd`) are missing.
docker-php-ext-configure gd --with-freetype --with-jpeg
docker-php-ext-install -j"$(nproc)" gd pdo_mysql zip opcache

curl -fsSL https://getcomposer.org/installer -o /tmp/composer-setup.php
php /tmp/composer-setup.php --install-dir=/usr/local/bin --filename=composer
rm /tmp/composer-setup.php

curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y --no-install-recommends nodejs

rm -rf /var/lib/apt/lists/*

mkdir -p /var/run/mysqld /var/lib/mysql
chown -R mysql:mysql /var/run/mysqld /var/lib/mysql

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# Tests/IntegrationTests is copied OUT of the repo first: TestDistribution is a
# directory inside the repo, so copying the repo into it would recurse.
cp -a /home/{pr.repo}/Tests/IntegrationTests {app_dir}

# Install the UNION of base and test-patch composer requirements. The test
# patch adds neos/neos-development-collection, without which composer resolves
# an inconsistent package set and `site:import` fails on an unconvertible
# `mixed` property. The run stages are offline, so this must happen here.
cd {dist_dir}
# Single line deliberately: a backslash continuation inside the single-quoted
# PHP source is passed through to PHP verbatim, where it is a parse error
# ("syntax error, unexpected token \\").
php -r '$f="composer.json"; $j=json_decode(file_get_contents($f), true); $j["require"]=array_merge(["neos/neos-development-collection"=>"8.3.x-dev","cweagans/composer-patches"=>"^1.7.3"], $j["require"]); $j["config"]["allow-plugins"]["cweagans/composer-patches"]=true; file_put_contents($f, json_encode($j, JSON_PRETTY_PRINT|JSON_UNESCAPED_SLASHES));'
composer update --no-interaction --no-progress -q || true
# `|| true` above, then assert. A bare failure under `set -e` gives a Docker
# error with no indication of *which* half of a two-runtime environment broke;
# a half-installed distribution that builds green is worse still. Tolerating the
# exit code and checking the artefact instead names the failure precisely.
if [ ! -d Packages/Libraries/neos ]; then
    echo "composer install produced no Neos packages" >&2
    exit 1
fi

# The repo becomes a package inside the distribution.
mkdir -p {dist_dir}/Packages/Application
rm -rf {ui_dir}
cp -a /home/{pr.repo} {ui_dir}
rm -rf {ui_dir}/Tests/IntegrationTests/TestDistribution/Packages

# Build the UI bundle the browser loads. yarn install is offline-capable: the
# repo commits a zero-install cache and .yarnrc.yml sets nodeLinker: node-modules.
cd {ui_dir}
corepack enable || true
yarn install --immutable || true
if [ ! -d node_modules ]; then
    echo "yarn install produced no node_modules" >&2
    exit 1
fi
node esbuild.js --production --e2e-testing
test -f Resources/Public/Build/Host.js

# Seed the database and import the E2E site fixture.
mkdir -p /var/run/mysqld /var/lib/mysql && chown -R mysql:mysql /var/run/mysqld /var/lib/mysql
mariadbd-safe --user=mysql > /tmp/mariadb.log 2>&1 &
for i in $(seq 1 90); do mariadb-admin ping --silent 2>/dev/null && break; sleep 1; done
# Flow connects over TCP as root@127.0.0.1; MariaDB's packaged root uses
# unix_socket auth and has no TCP grant, so both are created explicitly.
mariadb -e "ALTER USER 'root'@'localhost' IDENTIFIED VIA mysql_native_password USING PASSWORD('not_a_real_password'); CREATE DATABASE IF NOT EXISTS neos CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY 'not_a_real_password'; GRANT ALL ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION; FLUSH PRIVILEGES;"

cd {dist_dir}
./flow doctrine:migrate
./flow user:create --username=admin --password=password --first-name=John --last-name=Doe --roles=Administrator || true

mkdir -p DistributionPackages
rm -rf DistributionPackages/Neos.TestNodeTypes DistributionPackages/Neos.TestSite
ln -s "../../SharedNodeTypesPackage" DistributionPackages/Neos.TestNodeTypes
ln -s "../../Fixtures/{fixture}/SitePackage" DistributionPackages/Neos.TestSite
composer reinstall neos/test-nodetypes --no-interaction -q
composer reinstall neos/test-site --no-interaction -q
./flow flow:cache:flush --force
./flow flow:cache:warmup
./flow site:import --package-key=Neos.TestSite
./flow resource:publish

mariadb-admin -h 127.0.0.1 -u root -pnot_a_real_password shutdown || true

# The graded stages apply patches to the ORIGINAL clone, so it must be pristine
# here. Composer and yarn wrote only into the copy under the distribution and
# into gitignored paths.
#
# This is deliberately the last command: no `exit 0` follows it, so the script's
# exit status *is* this check's status and a dirty tree fails the build.
cd /home/{pr.repo}
bash /home/check_git_changes.sh
""".format(
                    pr=self.pr,
                    app_dir=APP_DIR,
                    dist_dir=DIST_DIR,
                    ui_dir=UI_DIR,
                    fixture=E2E_FIXTURE,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

# The graded copy of the repo is the one inside the distribution; patches are
# applied there so the running application picks them up.
{sync}

{run_e2e}
""".format(pr=self.pr, sync=_sync_cmd(), run_e2e=_RUN_E2E),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

if ! git apply --whitespace=nowarn {excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{sync}

{run_e2e}
""".format(
                    pr=self.pr,
                    excludes=_APPLY_EXCLUDES,
                    sync=_sync_cmd(),
                    run_e2e=_RUN_E2E,
                ),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
cd /home/{pr.repo}

# Canonical stage order: gold tests first, fix patch on top.
if ! git apply --whitespace=nowarn {excludes} /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

# At evaluation time this patch is the *agent's*, so it is applied with every
# gold test file excluded -- a fix patch that edits the tests grading it cannot
# take effect. The gold fix patch touches none of those paths, so the
# exclusions are a no-op for dataset generation.
if ! git apply --whitespace=nowarn {excludes} {gold_excludes} /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi

{sync}

{run_e2e}
""".format(
                    pr=self.pr,
                    excludes=_APPLY_EXCLUDES,
                    gold_excludes=gold_excludes,
                    sync=_sync_cmd(),
                    run_e2e=_RUN_E2E,
                ),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
RUN bash /home/prepare.sh

{self.clear_env}

"""


def _sync_cmd() -> str:
    """Mirror the patched clone into the distribution's package slot.

    Patches are applied to ``/home/<repo>`` -- the pristine clone the harness
    knows about and that ``check_git_changes.sh`` guards -- but the application
    the browser talks to runs from the copy under ``TestDistribution``. Only the
    PHP classes and the e2e specs need to move across; the built UI bundle,
    ``node_modules`` and the composer tree stay put, which is what keeps the run
    stages offline and fast.
    """
    return (
        f"rsync -a --delete Classes/ {UI_DIR}/Classes/\n"
        f"rsync -a --delete Tests/IntegrationTests/Fixtures/ "
        f"{UI_DIR}/Tests/IntegrationTests/Fixtures/"
    )


_JSON_BEGIN = "##### MSWEB-TESTCAFE-JSON"
_JSON_END = "##### MSWEB-TESTCAFE-STDERR"


def _json_blocks(clean: str) -> list[str]:
    """Candidate JSON documents in a stage log, most reliable first.

    The runner brackets the report between two markers, so the whole region
    between them is exactly one JSON document -- that is tried first and is what
    succeeds in practice.

    The brace scan is only a fallback, and it is **string-aware**. A naive
    depth counter breaks on this reporter: a failing test's ``errs`` entry
    embeds the page's own markup and script, braces included, so a `}` inside a
    string value looks like the end of the document and the scan splits
    mid-JSON. That failed silently on exactly one stage -- the TEST stage, the
    only one guaranteed to contain failures -- and produced a 0/0/0 result that
    still passed `report.check` as n2p.
    """
    begin = clean.find(_JSON_BEGIN)
    if begin != -1:
        end = clean.find(_JSON_END, begin)
        region = clean[begin + len(_JSON_BEGIN) : end if end != -1 else len(clean)]
        region = region.strip()
        if region:
            return [region]

    blocks: list[str] = []
    depth = 0
    start = None
    in_string = False
    escaped = False
    for i, ch in enumerate(clean):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(clean[start : i + 1])
                start = None
    return blocks


def parse_testcafe_json(test_log: str) -> TestResult:
    """Parse TestCafe's ``--reporter json`` output.

    Identity is ``<spec file>::<test name>``, with the spec path made
    repo-relative::

        Tests/IntegrationTests/Fixtures/1Dimension/issue-3184.e2e.js::Scenario #1: ...

    Both halves come straight from the reporter -- ``fixtures[].path`` and
    ``tests[].name`` -- so unlike the Mocha and Gradle configs no marker lines
    or filesystem lookups are needed. A test is failed when ``errs`` is
    non-empty, skipped when ``skipped`` is true, passed otherwise.

    The JSON is embedded in a marker-delimited region of a larger log (server
    status, exit code, stderr tail), so balanced top-level braces are scanned
    for rather than handing the whole log to ``json.loads``.

    A log with no parseable object -- a browser that never launched, a server
    that never came up -- yields an empty 0/0/0 result, which
    ``generate_report`` rejects rather than silently scoring as "nothing
    regressed".
    """
    clean = ANSI_ESCAPE.sub("", test_log)

    passed: set[str] = set()
    failed: set[str] = set()
    skipped: set[str] = set()

    blocks = _json_blocks(clean)

    for block in blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or "fixtures" not in data:
            continue

        for fixture in data.get("fixtures") or []:
            if not isinstance(fixture, dict):
                continue
            path = _repo_relative(fixture.get("path") or "")
            for test in fixture.get("tests") or []:
                if not isinstance(test, dict):
                    continue
                name = test.get("name") or ""
                if not name:
                    continue
                identity = f"{path}::{name}" if path else name

                if test.get("skipped"):
                    skipped.add(identity)
                elif test.get("errs"):
                    failed.add(identity)
                else:
                    passed.add(identity)

    passed -= failed
    passed -= skipped
    skipped -= failed

    return TestResult(
        passed_count=len(passed),
        failed_count=len(failed),
        skipped_count=len(skipped),
        passed_tests=passed,
        failed_tests=failed,
        skipped_tests=skipped,
    )


def _repo_relative(path: str) -> str:
    """Trim TestCafe's absolute spec path back to a repo-relative one.

    The reporter emits the path inside the distribution
    (``.../Packages/Application/Neos.Neos.Ui/Tests/...``). Anchoring on
    ``Tests/IntegrationTests`` keeps the identity stable regardless of where the
    image happens to place the package, which matters because the identity has
    to match across all three stages.
    """
    path = (path or "").replace("\\", "/")
    marker = "Tests/IntegrationTests/"
    idx = path.find(marker)
    if idx >= 0:
        return path[idx:]
    return path.rsplit("/", 1)[-1] if path else ""


@Instance.register("neos", "neos-ui")
class NeosUi(Instance):
    """Instance handler for neos/neos-ui.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create``
    resolves on.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return NeosUiImageDefault(self.pr, self._config)

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
        return parse_testcafe_json(test_log)
