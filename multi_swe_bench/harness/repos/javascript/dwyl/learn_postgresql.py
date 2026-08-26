"""Repo config for dwyl/learn-postgresql (JavaScript / node-tap 12 + PostgreSQL).

What this PR is
---------------
PR #52 ("Adding Basic Example for #51") is an *initial project* commit. The base
tree at ``f2209a776476d17c6b7bd1fe95353eecf7d0b5d0`` contains exactly two files::

    .gitignore
    README.md

Everything else -- ``package.json``, ``schema.sql``, ``server/*.js``, the client
and the docs -- is created by the fix patch. The gold test patch adds
``test/*.test.js`` plus ``test/fixtures/``. Every unusual decision below follows
from that single fact: **there is no project at BASE_COMMIT**, so nothing about
the environment can be derived from the checked-out tree at image-build time.

Consequences, in order:

* ``prepare.sh`` cannot run ``npm install`` -- there is no ``package.json`` to
  install from. The dependency set is therefore spelled out literally, mirroring
  the ``package.json`` the fix patch creates (see "Dependencies" below).
* The dependencies cannot be installed *into* the work tree either: ``npm
  install`` in a directory without a manifest writes ``package-lock.json``, and
  on some npm versions a ``package.json``. Both would collide with the fix
  patch, which creates ``package.json`` itself -- ``git apply`` fails with
  "already exists" and the fix stage dies before a single test runs. They are
  installed in ``/home/deps`` instead and reached through a symlink at
  ``/home/learn-postgresql/node_modules``, a path ``.gitignore`` already covers
  at BASE_COMMIT, so the work tree stays clean for ``check_git_changes.sh``.
* The baseline stage legitimately reports **zero tests**: ``run.sh`` applies no
  patches, so ``test/`` does not exist and tap prints ``1..0``. That is the
  correct baseline here, not a broken script, and it is also what makes
  ``Report.check()`` rule 4 (PASS in run -> NONE/SKIP in test -> FAIL in fix)
  unreachable for this instance: nothing can be PASS in a stage with no tests.

Node 12 is not a style choice
-----------------------------
``package.json`` pins ``"pg": "^7.8.1"``. ``pg`` 7.18.2 opens its connection in
``lib/connection.js``::

    if (this.stream.readyState === 'closed') { this.stream.connect(port, host) }
    else if (this.stream.readyState === 'open') { this.emit('connect') }

Node changed what a freshly constructed ``net.Socket()`` reports. Measured
2026-08-25 in these exact images::

    node:10-buster      v10.24.1   readyState = closed
    node:12.22.12-*     v12.22.12  readyState = closed
    node:14-bullseye    v14.21.3   readyState = open

On Node >= 14 the ``'open'`` branch is taken: ``Connection`` emits ``connect``
*synchronously*, before ``Client._connect`` has attached its listener, so the
startup packet is never written. The socket is genuinely connected -- a raw
``net.connect(5432)`` succeeds in the same container -- but ``bytesWritten``
stays at ``0`` and ``client.connect()``'s callback never fires. Every
``db.*`` test then reports ``not ok 1 - test unfinished`` and the whole file is
abandoned with "child test left in queue". That was the observed failure on
node:14 before the base image was moved; it is silent, looks exactly like a bad
fix patch, and is entirely a Node/pg version mismatch.

``node:12.22.12-bullseye`` is the pinned base. 12.22.12 is the final Node 12
release, so the tag is immutable; bullseye rather than buster because Debian
buster has been moved to ``archive.debian.org`` and ``apt-get update`` against
``deb.debian.org`` now fails with "does not have a Release file" -- measured, a
buster-based build cannot install PostgreSQL at all. Node 12 was also the active
LTS line when this PR merged (2020-12-31).

Dependencies
------------
``package.json`` (created by the fix patch) declares seven packages. Three are
installed, at the exact versions its semver ranges resolve to:

    tap@12.6.1        ^12.6.1   the test runner
    pg@7.18.2         ^7.8.1    required by server/db.js
    supertest@4.0.2   ^4.0.2    required by test/server.test.js

The other four are deliberately absent and none is reachable from the graded
files: ``nyc``/``tap-nyc`` only implement the ``npm test`` coverage wrapper,
which is bypassed (see "Why not `npm test`"); ``faster`` is a file-watching dev
tool; ``github-scraper`` is required only by ``server/bot.js``, which only
``test/bot.test.js`` loads (see "Why bot.test.js is not graded").

Why not ``npm test``
--------------------
``package.json`` defines::

    "test": "nyc tap ./test/*.test.js | tap-nyc"
    "postinstall": "npm run recreate"

Three separate problems. (1) ``npm test`` cannot run in the baseline or test
stage at all -- ``package.json`` does not exist there -- so it could not be the
same command in all three scripts, which is the one property the f2p comparison
depends on. (2) The ``nyc`` block sets ``check-coverage`` with 100% line,
statement, function and branch thresholds, so the command's exit status reports
coverage, not tests. (3) ``tap-nyc`` is a pretty-printer: it replaces the TAP
stream with a spec-style summary and drops the nesting ``parse_log`` reads.
The runner is therefore invoked directly, identically in all three scripts, and
the ``postinstall`` database bootstrap (``drop``/``create``/``schema``) is
performed by the scripts themselves against the same ``codeface`` database.

Why ``bot.test.js`` is not graded
---------------------------------
The graded command names three files explicitly rather than globbing
``./test/*.test.js``. The fourth, ``test/bot.test.js``, is excluded, and the
reason is not that it fails -- it is that it cannot fail *stably*:

* Its helper ``test/fixtures/make-fixture.js`` does
  ``fs.writeFileSync('./test/fixtures/' + name, JSON.stringify(data))`` on every
  crawl result. tap runs files serially in glob order, so ``bot.test.js`` runs
  **before** ``db.test.js`` and ``utils.test.js`` and overwrites the very
  fixtures they read (``person.json``, ``org.json``, ``repo.json``,
  ``stargazers.json``). ``server/bot.js`` passes ``(error, data)`` straight
  through on failure, where ``data`` is ``undefined``, and
  ``JSON.stringify(undefined)`` is ``undefined`` -- so a failed crawl writes the
  literal text ``undefined`` into a fixture and the two downstream files die on
  ``JSON.parse``. A network hiccup in one stage would silently change other
  tests' results in that stage only.
* Those crawls are live ``https://github.com`` scrapes performed by
  ``github-scraper``@6 against markup from 2019, inside a graded stage. Even
  when they "work" the assertions are counts of real followers
  (``t.equal(data.entries.length, 4)``), which change without notice.

Measured 2026-08-25, ``bot.test.js`` currently cannot even load: installing
``github-scraper@6`` on Node 12 resolves ``htmlparser2`` -> ``entities`` to an
ESM-only release and the file aborts at require time with ``ERR_REQUIRE_ESM``,
reporting ``1..0 # no tests found``. So today it is a permanent FAIL in both
graded stages and contributes nothing; the day the dependency tree resolves
differently it becomes a live-network coupling to two other files' fixtures.
Excluding it costs no signal -- ``server.test.js`` and ``utils.test.js`` are
both FAIL -> PASS across the graded transition, and every ``db.test.js`` subtest
is NONE -> PASS -- and removes the only nondeterminism in the suite.

PostgreSQL
----------
``.travis.yml`` (added by the fix patch) is the upstream recipe and is
reproduced rather than invented::

    services: [postgresql]
    env: DATABASE_URL=postgres://postgres:@localhost/codeface, NODE_ENV=TEST

The container has no service manager, so each script starts the cluster itself
with ``pg_ctlcluster`` and waits on ``pg_isready``. Debian's default
``pg_hba.conf`` authenticates local TCP with ``scram-sha-256``; the Travis
connection string carries an empty password, so the auth methods are rewritten
to ``trust`` -- the equivalent of Travis's own passwordless ``postgres``
superuser, in a throwaway container with the cluster bound to loopback only.

``localhost`` is kept verbatim from ``.travis.yml`` rather than replaced with
``127.0.0.1``: both were measured to connect on Node 12, whose default DNS
ordering puts the IPv4 entry first.

``schema.sql`` is guarded by ``[ -f schema.sql ]`` because the file is created
by the fix patch and genuinely does not exist in the other two stages. The guard
is byte-identical in all three scripts, so it is not a per-stage difference.

Test identity
-------------
tap 12 selects its reporter by TTY -- ``docker_client.containers.run`` detaches,
so raw TAP is already the default -- but ``--reporter=tap`` is passed explicitly
so the format cannot change under a different invocation. ``--no-coverage``
keeps tap's bundled nyc out of the stream, and ``-j1`` pins serial execution so
file order, and therefore the duplicate-name ordinals described below, are
deterministic.

``parse_log`` reports **subtests only** -- the ``tap.test('...')` level and the
file level -- and deliberately ignores bare assertions. That is a correctness
requirement, not a simplification: assertion descriptions in this suite embed
per-run values. ``test/db.test.js`` seeds a URL with
``Math.floor(Math.random() * 100000)`` and asserts
``t.equal(..., 'next_page is: ' + result.rows[0].next_page)``, which prints as
``ok 1 - next_page is: /dwyl60829`` -- a different name in every stage of every
run. Capturing it would union into three distinct entries across the three
stages and manufacture exactly the NONE/FAIL transitions ``Report.check()``
rejects. The five trailing ``not ok N - Cannot read property 'rows' of
undefined`` assertions tap emits after ``db.end()`` are the same hazard in the
other direction: five results sharing one description, which would collapse to
one entry.

Subtest results are told apart from assertions structurally, not heuristically:
tap precedes every subtest's result line with a ``# Subtest: <name>`` header at
the *same* indentation, and assertions have no such header. Names are the full
path (``file > test``), because a leaf name is not unique -- ``db.test.js``
declares ``'db.select_next_page selects next_page to be viewed'`` twice, at
lines 9 and 27. Even the full path collides for those two, so a repeated path
gets a ``(#N)`` suffix; without it the two would merge and one graded result
would vanish.

``# time=...`` is stripped from every name. It is the only variable metadata tap
appends to a result line, and leaving it in would give the same test a different
name in each stage -- the classic source of the PASS -> NONE -> FAIL anomaly.
ANSI escapes are stripped first: ``server/utils.js`` writes its error banner with
hard-coded ``\\x1b[44m\\x1b[33m\\x1b[1m``, so the stream is coloured regardless
of TTY.

Measured baseline (2026-08-25, all three stages, x86_64)
--------------------------------------------------------
    run   1..0                                    -> 0 passed / 0 failed
    test  3 files, all `not ok` (Cannot find module '../server/*')
                                                  -> 0 passed / 3 failed
    fix   db.test.js not ok, server/utils ok,
          11 subtests                             -> 12 passed / 2 failed

The two fix-stage failures are honest, reproducible defects in the gold code,
not environment problems: ``insert_relationships`` trips a ``TypeError: Cannot
read property 'id' of undefined`` at ``server/db.js:174``, which fails its
parent file ``./test/db.test.js`` as well. Both are FAIL in the test stage too,
so neither is a PASS -> FAIL regression.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# Shared, verbatim body of run.sh / test-run.sh / fix-run.sh.
#
# Intentionally a plain constant with no substitutions: the only text that
# differs between the three scripts is the `git apply` line in their preambles,
# so the graded portion is byte-identical by construction rather than by review.
# Anything that varied here would make a FAIL -> PASS transition attributable to
# the command instead of to the fix patch.
_TEST_BODY = """
# --- PostgreSQL -------------------------------------------------------------
# .travis.yml declares `services: [postgresql]`; a container has no service
# manager, so the cluster is started by hand. The version is discovered rather
# than hard-coded so a base-image bump does not silently skip this block.
PG_VERSION="$(ls /etc/postgresql | sort -V | tail -1)"
PG_HBA="/etc/postgresql/${PG_VERSION}/main/pg_hba.conf"

# Debian defaults local TCP auth to scram-sha-256. The DATABASE_URL that
# .travis.yml and server/db.js both use carries an empty password, so every
# connection would be rejected. `trust` reproduces Travis's passwordless
# postgres superuser; the cluster listens on loopback only.
sed -i -E 's/^((local|host)[[:space:]]+.*[[:space:]]+)(peer|ident|md5|scram-sha-256)[[:space:]]*$/\\1trust/' "$PG_HBA"

pg_ctlcluster "${PG_VERSION}" main start

for _ in $(seq 1 60); do
    if pg_isready -q -h 127.0.0.1 -p 5432; then break; fi
    sleep 1
done

# package.json's `postinstall` is `npm run recreate` (drop + create + schema).
# npm install never runs in a graded stage, so the bootstrap happens here.
psql -h 127.0.0.1 -U postgres -q -c 'DROP DATABASE IF EXISTS codeface;'
psql -h 127.0.0.1 -U postgres -q -c 'CREATE DATABASE codeface;'

# schema.sql is created by the fix patch and genuinely absent in the other two
# stages. The guard is identical in all three scripts, so it is not a per-stage
# difference in the command.
if [ -f schema.sql ]; then
    psql -h 127.0.0.1 -U postgres -d codeface -q -f schema.sql
fi

# --- graded suite -----------------------------------------------------------
# node_modules is a symlink to /home/deps/node_modules planted by prepare.sh,
# so ./node_modules/.bin/tap resolves in every stage, including the baseline
# where the repo has no package.json at all.
#
# --reporter=tap  pin the raw TAP stream parse_log reads
# --no-coverage   keep tap's bundled nyc out of that stream
# -j1             serial, so file order (and duplicate-name ordinals) are fixed
# -t60            per-file cap; the slowest graded file measured 1.4s
#
# test/bot.test.js is excluded on purpose -- see the module docstring.
#
# `set +e` rather than `|| true`: a non-zero tap exit is the expected, honest
# outcome of the test stage, but the failure must still be visible. The plan
# assertion below is what turns "the runner never started" into a hard error
# instead of a silent 0/0/0 report.
set +e
timeout --kill-after=30 600 ./node_modules/.bin/tap \\
    --reporter=tap --no-coverage -j1 -t60 \\
    ./test/db.test.js ./test/server.test.js ./test/utils.test.js \\
    > /tmp/tap.out 2>&1
TAP_RC=$?
set -e
if [ "$TAP_RC" -ne 0 ]; then
    echo "NOTE: tap exited ${TAP_RC}"
fi

# parse_log reads stdout.
cat /tmp/tap.out

# Runner-start guarantee: tap always emits a plan (`1..N`, including `1..0`)
# when it runs. No plan means it never started -- fail the stage loudly rather
# than hand parse_log an empty log that reports 0/0/0.
grep -qE '^1\\.\\.[0-9]+' /tmp/tap.out
"""


class LearnPostgresqlImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Node 12.22.12 plus a PostgreSQL cluster.

    Tagged per PR rather than with a shared ``:base`` so that another instance
    of this repo cannot rewrite the foundation an already-verified instance was
    built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into clone + ``WORKDIR`` +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD``, and
    supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args. Nothing is emitted
    after the clone line for exactly that reason: the enhancer appends ``CMD``
    there and any later instruction would be stranded below it.
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
        # Fully pinned: 12.22.12 is the last Node 12 release, so the tag never
        # moves. Node >= 14 breaks pg@7's connection handshake (see docstring),
        # and Debian buster -- the only distro the node:12 line otherwise ships
        # -- is archived, so its apt repositories no longer resolve.
        return "node:12.22.12-bullseye"

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

        # postgresql / postgresql-contrib supply the server, `psql`,
        # `pg_ctlcluster` and `pg_isready` the run scripts drive. On bullseye
        # this is PostgreSQL 13; schema.sql uses only SERIAL, VARCHAR, TIMESTAMP
        # and REFERENCES, so no version-specific feature is involved.
        # Everything here is arch-neutral: Debian publishes these packages for
        # amd64 and arm64 alike, and nothing is fetched by direct download.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    postgresql postgresql-contrib \\
    ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class LearnPostgresqlImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and provisions node_modules out-of-tree."""

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
        return LearnPostgresqlImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
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
set -e

cd /home/{pr.repo}
git reset --hard
git clean -fdx
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh

# The work tree at BASE_COMMIT is `.gitignore` + `README.md` -- there is no
# package.json to install from, so the dependency set is spelled out. It mirrors
# the manifest the fix patch creates, at the versions its semver ranges resolve
# to:
#     tap      ^12.6.1  -> 12.6.1   the runner
#     pg       ^7.8.1   -> 7.18.2   server/db.js
#     supertest ^4.0.2  -> 4.0.2    test/server.test.js
# The remaining four declared packages are unreachable from the graded files:
# nyc/tap-nyc only implement the `npm test` coverage wrapper, `faster` is a
# file watcher, and github-scraper is required only by server/bot.js, which
# only the ungraded test/bot.test.js loads.
#
# Installed in /home/deps, NOT in the work tree: `npm install` without a
# manifest writes package-lock.json (and on some npm versions package.json),
# and the fix patch creates package.json itself -- `git apply` would abort with
# "already exists" and the fix stage would produce no results at all.
mkdir -p /home/deps
cd /home/deps
npm init -y > /dev/null

# `|| true` as required for install steps: a native-module compile failure on
# arm64 must not abort the image build. Real breakage surfaces as test results
# in the graded runs, and the symlink assertion below still fires.
npm install --no-audit --no-fund --loglevel=error \\
    tap@12.6.1 pg@7.18.2 supertest@4.0.2 || true

# Reached from /home/{pr.repo}/test/*.test.js by ordinary node resolution, and
# from the run scripts as ./node_modules/.bin/tap. `node_modules` is already in
# .gitignore at BASE_COMMIT, so the link is invisible to `git status` and to
# check_git_changes.sh, and to the `git apply` in every run script.
ln -sfn /home/deps/node_modules /home/{pr.repo}/node_modules

# Not `|| true`: if the runner is missing, every graded stage would report
# 0/0/0 and the failure would only show up as an unexplained invalid report.
test -x /home/{pr.repo}/node_modules/.bin/tap

# No build step: this project is plain CommonJS run straight from source --
# there is no compile, bundle or transpile phase anywhere in package.json.

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
export NODE_ENV=TEST
export DATABASE_URL=postgres://postgres:@localhost/codeface

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY,
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        prepare_commands = "RUN bash /home/prepare.sh"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}

{prepare_commands}

{self.clear_env}

"""


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# tap 12's raw TAP stream. Both markers are anchored on their indentation,
# because indentation is the only thing carrying the subtest nesting:
#
#   # Subtest: ./test/db.test.js
#       # Subtest: insert_org
#           ok 1 - org.uid 11708465          <- assertion, no header: ignored
#           1..2
#       ok 4 - insert_org # time=170.353ms   <- subtest result, header above
#   not ok 1 - ./test/db.test.js # time=1395.13ms
_SUBTEST_HEADER = re.compile(r"^(\s*)#\s*Subtest:\s*(.+?)\s*$")
_RESULT_LINE = re.compile(r"^(\s*)(not ok|ok)\b\s*\d*\s*-?\s*(.*?)\s*$")

# The only variable metadata tap appends to a result line. Left in place it
# would give the same test a different name in every stage.
_TIME_SUFFIX = re.compile(r"\s*#\s*time=[0-9.]+\s*m?s\s*$")
_DIRECTIVE = re.compile(r"\s*#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

# TAP YAML diagnostic blocks. They are skipped wholesale: they reproduce failing
# source lines verbatim, and a reproduced line can look exactly like a result.
_YAML_OPEN = re.compile(r"^\s*---\s*$")
_YAML_CLOSE = re.compile(r"^\s*\.\.\.\s*$")


def parse_tap_log(log: str) -> TestResult:
    """Turn tap 12's raw TAP stream into ``file > subtest`` results.

    Only subtests are reported. A result line is a subtest iff tap printed a
    ``# Subtest: <name>`` header at the same indentation earlier -- assertions
    never carry one. See the module docstring for why assertion descriptions in
    this suite are unusable as identities.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # server/utils.js hard-codes its error banner as \x1b[44m\x1b[33m\x1b[1m,
    # so the stream is coloured whether or not a TTY is attached.
    clean = ANSI_ESCAPE.sub("", log)

    # (indent, name) for every subtest currently open, outermost first.
    stack: list[tuple[int, str]] = []
    # How many times a completed path has been seen, so repeats stay distinct.
    seen: dict[str, int] = {}
    in_yaml = False

    for raw in clean.splitlines():
        line = raw.rstrip("\r")

        if in_yaml:
            if _YAML_CLOSE.match(line):
                in_yaml = False
            continue
        if _YAML_OPEN.match(line):
            in_yaml = True
            continue

        header = _SUBTEST_HEADER.match(line)
        if header:
            indent = len(header.group(1))
            # A sibling opening at this depth means anything still open at or
            # below it never produced a result; drop it rather than nest under.
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, header.group(2)))
            continue

        result = _RESULT_LINE.match(line)
        if not result:
            continue

        indent = len(result.group(1))
        status = result.group(2)
        name = result.group(3)

        skipped = bool(_DIRECTIVE.search(name))
        name = _DIRECTIVE.sub("", name)
        name = _TIME_SUFFIX.sub("", name).strip()

        # The structural test: this closes an open subtest, or it is a bare
        # assertion and carries no identity worth recording.
        if not stack or stack[-1][0] != indent or stack[-1][1] != name:
            continue

        path = " > ".join(entry for _, entry in stack)
        stack.pop()

        # db.test.js declares 'db.select_next_page selects next_page to be
        # viewed' twice (lines 9 and 27), so even the full path repeats. Without
        # a suffix the two would merge and one graded result would disappear.
        count = seen.get(path, 0) + 1
        seen[path] = count
        if count > 1:
            path = f"{path} (#{count})"

        if skipped:
            skipped_tests.add(path)
        elif status == "ok":
            passed_tests.add(path)
        else:
            failed_tests.add(path)

    # TestResult.__post_init__ rejects overlapping sets. Failure is the honest
    # verdict wherever a name would otherwise appear twice.
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


@Instance.register("dwyl", "learn-postgresql")
class LearnPostgresql(Instance):
    """Instance handler for dwyl/learn-postgresql.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on. The org is spelled exactly as the dataset does.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return LearnPostgresqlImageDefault(self.pr, self._config)

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

    def parse_log(self, log: str) -> TestResult:
        return parse_tap_log(log)
