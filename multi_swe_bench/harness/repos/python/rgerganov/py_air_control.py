"""Repo config for rgerganov/py-air-control (Python / pytest).

What is actually graded
-----------------------
PR #46 ("Testing", closing issue #35 "Unable to get CoAP to work with
AC2889/60") is the commit that gives this project a test suite at all. The gold
test patch creates the entire ``testing/`` tree from nothing:

    test-requirements.txt
    testing/{coap_resources,coap_test_server,http_test_controller,
             http_test_server,plain_coap_resources}.py
    testing/{test_coap,test_http,test_plain_coap}.py
    testing/data.json

and the gold fix patch is the client-side change those tests exercise --
``pyairctrl/{airctrl,coap_client,http_client,plain_coap_client}.py``.

That shape matters for the stage matrix. At ``base.sha`` there is **no
``testing/`` directory**, so the ``run`` stage collects zero tests and every
graded name is ``NONE`` there. The signal therefore lives entirely in the
``test`` -> ``fix`` transition, which is what ``Report.check()`` rule 3 grades
anyway (``test != PASS and fix == PASS``).

``Report.check()`` rule 5 (the cheating guard) is clean by construction: the
test patch touches only ``test-requirements.txt`` and ``testing/**`` while the
fix patch touches ``pyairctrl/**`` plus ``.gitignore``, ``.travis.yml``,
``README.md``, ``Examples.md`` and ``create_example_page.py`` --
``set(fix_patch_files) & set(test_patch_files)`` is empty.

The port-80 problem, and why ``iptables`` is not an option
----------------------------------------------------------
``testing/test_http.py`` starts a Flask server on **127.0.0.1:5000**::

    self.httpServer = HttpTestServer(5000)

but the code under test builds its URLs with no port at all
(``pyairctrl/http_client.py``)::

    url = "http://{}/di/v1/products/0/security".format(self._host)

so ``urllib.request`` dials **port 80**. Upstream CI bridges that gap with a
NAT rule -- the fix patch adds this line to ``.travis.yml``::

    sudo iptables -t nat -I OUTPUT -p tcp -o lo --dport 80 -j REDIRECT --to-ports 5000

That is unavailable here. ``docker_util.run`` calls
``docker_client.containers.run(...)`` with no ``cap_add`` and no
``privileged`` (utils/docker_util.py:314-323), so the graded stages have no
``NET_ADMIN`` and cannot touch netfilter at all. Without a substitute every
HTTP test fails with ``ConnectionRefusedError`` and the instance looks broken
rather than misconfigured.

The substitute is a userspace forwarder, which needs no capabilities::

    socat TCP-LISTEN:80,fork,reuseaddr TCP:127.0.0.1:5000

It is started and torn down inside the shared graded body, so all three stages
see an identical environment.

Only HTTP needs this. ``testing/test_coap.py`` and
``testing/test_plain_coap.py`` start ``CoAPTestServer(5683)`` and their clients
target the CoAP default port 5683, so client and server already agree -- no
redirect, and nothing to do for them.

Local sources win over the installed package
--------------------------------------------
``prepare.sh`` runs ``pip install .`` once, exactly as ``.travis.yml`` does. That
would normally be a hazard: the graded stages patch ``pyairctrl/**`` in the work
tree, and if the tests imported the *installed* copy from ``site-packages`` the
fix patch would change nothing observable.

It does not happen here because the suite is invoked as ``python -m pytest``
from the repo root, and ``python -m`` puts the current directory at
``sys.path[0]`` -- ahead of ``site-packages``. ``import pyairctrl`` therefore
resolves to the patched work tree. This is also what upstream CI relies on:
``.travis.yml`` installs the package and then runs ``coverage run -m pytest``
from the same directory. No reinstall is needed in the graded body, and adding
one would only slow every stage down.

Test identity
-------------
The suite is run with ``pytest -v``, which emits one line per test::

    testing/test_http.py::TestHTTP::test_get_valid_session_key PASSED [ 10%]
    testing/test_coap.py::TestCoAP::test_set_values FAILED           [ 20%]

Names are ``<file>::<class>::<test>`` -- the LOW-risk shape in the audit's
framework table, inherently unique because the file path is part of the name,
and free of any per-run value. The trailing ``[ NN%]`` progress counter *is*
variable between stages (it is a fraction of the collected total, which differs
once the fix patch lands), so ``parse_log`` anchors on the status keyword and
never lets the percentage reach a name. An unstripped percentage would make the
same test a different name in each stage, which surfaces as the
``PASS -> NONE -> FAIL`` anomaly ``Report.check()`` rule 4 rejects.

``-p no:cacheprovider`` keeps pytest from writing ``.pytest_cache`` into the work
tree; that directory is not in the repo's ``.gitignore`` at this commit, and a
stray untracked directory would dirty ``git status --porcelain`` for
``check_git_changes.sh``.

Toolchain
---------
``python:3.8-bullseye``. ``setup.py`` refuses anything below 3.4
(``sys.exit("Python 3.4 or newer is required.")``) and ``.travis.yml`` at this
commit tests 3.4 through 3.8, so 3.8 is the newest interpreter the PR was
actually exercised against. The ``-bullseye`` variant rather than ``-slim``:
``requirements.txt`` pins ``CoAPthon3`` to a **git commit**::

    CoAPthon3 @ git+https://github.com/Tanganelli/CoAPthon3@89d5173

so pip has to be able to clone during install, and the full ``python:*-bullseye``
images derive from ``buildpack-deps`` and already ship ``git``. Only ``socat`` is
added on top.

``test-requirements.txt`` (``coveralls``, ``flask``, ``pytest``) does not exist
until the test patch is applied, so ``prepare.sh`` installs those dependencies by
name instead of by file -- otherwise the ``run`` stage would try to read a file
that is not there. ``coveralls`` is deliberately skipped: it only uploads
coverage to a third-party service and needs a repo token, and nothing in the
graded run reads it. ``requests`` is installed explicitly because
``testing/http_test_server.py`` imports it directly while
``test-requirements.txt`` only gets it transitively through ``coveralls``.
"""

import re

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One pytest invocation, defined once so the three graded scripts cannot drift.
# -v            : one `file::class::test STATUS` line per test, which is what
#                 parse_log keys on. Without it pytest prints only dots.
# -p no:cacheprovider : keeps .pytest_cache out of the work tree (not gitignored
#                 at this commit; it would dirty check_git_changes.sh).
# `timeout` is mandatory, not defensive. The gold test fixtures leave the
# interpreter unable to exit: testing/coap_test_server.py runs its CoAP server on
# a non-daemon threading.Thread and testing/http_test_server.py on a
# multiprocessing.Process, and neither is reliably reaped in teardown. Measured
# 2026-08-25 in a real graded stage: pytest printed its complete summary
# ("4 failed, 28 passed ... in 12.58s") and the process was still alive 11 minutes
# later. docker_util.run streams the stage with follow=True and no timeout on this
# path, so an unreaped interpreter hangs the stage forever rather than failing it.
#
# Killing on the wall clock costs nothing: pytest has already flushed the full
# result block to /tmp/pytest.out by then, so parse_log still sees every test.
#
# 180s is chosen deliberately. Because the interpreter *always* hangs once the
# fixtures have run, this timeout is not an exceptional path -- it is how the
# test and fix stages normally end, so its value IS the per-stage wall clock.
# The measured suite takes 12.58s, so 180s leaves ~14x headroom on a loaded
# host while keeping a full three-stage grade near six minutes rather than the
# thirty that a 600s value would cost on every future run of this instance.
_PYTEST_CMD = (
    "timeout --kill-after=30 180 python -m pytest -v -p no:cacheprovider"
)

# Shared body of run.sh / test-run.sh / fix-run.sh. Identical in all three by
# construction: the only thing that differs between the graded stages is which
# patch was applied above this block. Anything that varied the command itself
# would make a FAIL -> PASS transition attributable to the command rather than
# to the fix.
_TEST_BODY = """\
# Runs in /home/{repo}; every caller cd's there first.

# Stand in for the `sudo iptables -t nat ... --dport 80 -j REDIRECT --to-ports
# 5000` rule the fix patch adds to .travis.yml. testing/test_http.py serves on
# 127.0.0.1:5000 while pyairctrl/http_client.py dials port 80, and the harness
# starts containers without NET_ADMIN (docker_util.py:314 passes no cap_add), so
# netfilter is off the table. A userspace forwarder needs no capabilities.
# Torn down again below so nothing outlives the stage.
socat TCP-LISTEN:80,fork,reuseaddr TCP:127.0.0.1:5000 > /tmp/socat.log 2>&1 &
SOCAT_PID=$!
for _ in $(seq 1 20); do
    if socat -u OPEN:/dev/null TCP:127.0.0.1:80 2>/dev/null; then break; fi
    sleep 1
done

# Deliberately non-fatal, then re-armed. The `test` stage is *supposed* to end
# non-zero -- the gold tests run against unfixed clients and fail, which is the
# graded signal -- and the `run` stage exits 5 ("no tests collected") because
# testing/ does not exist until the test patch lands. Aborting on either under
# `set -e` would cut the stage off before the log reached stdout, leaving
# parse_log with nothing and tripping Report.check() rule 1 on an instance that
# is in fact working.
#
# This does not weaken the failure signal -- the start-up assertion at the
# bottom is what guarantees a stage cannot silently report 0/0/0.
set +e
{pytest_cmd} > /tmp/pytest.out 2>&1
PYTEST_RC=$?
set -e

kill "$SOCAT_PID" 2>/dev/null || true

# parse_log reads stdout, so the captured suite output has to land there.
cat /tmp/pytest.out

if [ "$PYTEST_RC" -ne 0 ]; then
    echo "NOTE: pytest exited $PYTEST_RC; see the results above"
fi

# Start-up guarantee. A stage where the interpreter never got as far as
# collecting -- a missing pytest, an import error in conftest, a wiped venv --
# writes no session banner at all. Failing here surfaces that as a broken stage
# instead of an empty TestResult that looks like a legitimate 0/0/0.
grep -q "test session starts" /tmp/pytest.out
"""


class PyAirControlImageBase(Image):
    """Per-PR ``:base-pr-<N>`` image -- Python 3.8 plus socat.

    Tagged per PR rather than with a shared ``:base``: one shared tag would be
    rewritten by every other instance of this repo, silently changing the
    foundation an already-verified instance was built against.

    ``dependency()`` returns a string, so ``DockerfileEnhancer.enhance``
    rewrites the ``git clone`` below into the standard clone +
    ``checkout ${BASE_COMMIT}`` + ``Image._HARDENING_BLOCK`` + ``CMD`` sequence
    and supplies ``REPO_URL`` / ``BASE_COMMIT`` as build args. Nothing that
    matters is emitted after the clone line for exactly that reason -- the
    enhancer appends ``CMD`` there, and any later instruction would be stranded
    below it.
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
        # setup.py hard-exits below 3.4 and .travis.yml at this commit tests
        # 3.4-3.8, so 3.8 is the newest interpreter this PR was exercised
        # against. -bullseye, not -slim: requirements.txt installs CoAPthon3
        # from a git commit, so pip needs git, and the buildpack-deps-derived
        # full images already ship it.
        return "python:3.8-bullseye"

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

        # Only socat is added. git and the C toolchain already ship in the
        # buildpack-deps-derived python:*-bullseye image, and DEBIAN_FRONTEND /
        # LANG / TZ come from DockerfileEnhancer._ENV_BLOCK -- re-declaring any
        # of them here would only create two places to keep in sync.
        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    socat \\
    && rm -rf /var/lib/apt/lists/*

{code}

{self.clear_env}

"""


class PyAirControlImageDefault(Image):
    """Per-PR image -- pins BASE_COMMIT and installs the dependency set."""

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
        return PyAirControlImageBase(self.pr, self._config)

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

# `|| true` per house rule: a transient index failure here must not abort the
# image build, and the graded runs surface any real breakage as test results.
#
# requirements.txt pins CoAPthon3 to a git commit, so this step needs git --
# which the buildpack-deps-derived base already provides.
pip install -r requirements.txt || true

# Same `pip install .` .travis.yml performs. Harmless with respect to the
# patches: the suite runs as `python -m pytest` from the repo root, so
# sys.path[0] is the work tree and `import pyairctrl` resolves to the patched
# sources rather than this installed copy. See the module docstring.
pip install . || true

# test-requirements.txt (coveralls, flask, pytest) is CREATED by the gold test
# patch, so it cannot be installed by file here -- the run stage would be
# reading a path that does not exist yet. Install its contents by name instead.
#
# coveralls is deliberately omitted: it only uploads coverage to a third-party
# service and needs a repo token, and nothing in the graded run reads it.
# requests is explicit because testing/http_test_server.py imports it directly
# while test-requirements.txt only pulls it in transitively via coveralls.
pip install pytest flask requests || true

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{pr.repo}
if ! git -C /home/{pr.repo} apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
""".format(pr=self.pr)
                + _TEST_BODY.format(repo=self.pr.repo, pytest_cmd=_PYTEST_CMD),
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

# pytest -v, one line per test:
#   testing/test_http.py::TestHTTP::test_get_valid_session_key PASSED [ 10%]
#   testing/test_coap.py::TestCoAP::test_set_values FAILED            [ 20%]
#
# The name is captured as the `file::class::test` node id only. The trailing
# `[ NN%]` is a fraction of the collected total, which changes once the fix
# patch alters how many tests run -- letting it into a name would make the same
# test a different name in different stages.
_RESULT_LINE = re.compile(
    r"^(?P<name>\S+::\S+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
# pytest's own failure epilogue repeats node ids in a different shape
# (`_____ TestHTTP.test_x _____`, `FAILED testing/test_http.py::... - AssertionError`).
# Parsing past the summary header would double-count, so the scan stops there.
_SUMMARY_START = re.compile(r"^=+\s+(FAILURES|ERRORS|short test summary info|warnings summary)\b")


def parse_pytest_log(log: str) -> TestResult:
    """Key ``TestResult`` on pytest's ``-v`` node ids.

    ``file::class::test`` is inherently unique because the path is part of the
    name, so no suite-nesting reconstruction is needed here -- unlike the
    Mocha/Jasmine style reporters. The only variable metadata pytest puts on
    those lines is the trailing progress percentage, which the regex never
    captures. See the module docstring.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # pytest colourises when it thinks it has a TTY. Docker's log stream is not
    # one, so escapes should not appear -- but every pattern below is anchored
    # and would silently fail against them, so stripping is unconditional.
    clean = ANSI_ESCAPE.sub("", log)

    for raw in clean.splitlines():
        line = raw.rstrip()

        # Everything after the failure/summary banner is a restatement of
        # results already counted above.
        if _SUMMARY_START.match(line):
            break

        m = _RESULT_LINE.match(line)
        if not m:
            continue
        name, status = m.group("name"), m.group("status")
        if status in ("PASSED", "XPASS"):
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        else:  # SKIPPED, XFAIL
            skipped_tests.add(name)

    # TestResult.__post_init__ rejects overlapping sets. A test that errors in
    # teardown after passing is reported both ways; failure is the honest
    # verdict.
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


@Instance.register("rgerganov", "py-air-control")
class PyAirControl(Instance):
    """Instance handler for rgerganov/py-air-control.

    Registered under the bare ``org/repo`` key: the raw dataset carries neither
    ``tag`` nor ``number_interval``, which is what ``Instance.create`` resolves
    on. The repo name keeps its hyphens because the JSONL does.
    """

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image | None:
        return PyAirControlImageDefault(self.pr, self._config)

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
        return parse_pytest_log(log)
