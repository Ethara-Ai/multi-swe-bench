"""scrapy/scrapy PRs #2061-#3858 - ten PRs graded through the project's own pytest suite.

Every value below was read off the repo at the ten base commits, not inferred. The PR numbers
do not order by date: #2061 was opened in 2016 but sits on a 2018 commit, and #3559 stayed
open until 2021. What the image has to satisfy is the spread of the BASE commits, which runs
from Dec 2016 to Jun 2022:

  PR    base sha    base date   scrapy   python_requires      root reqs         test reqs
  2410  d19c4c1f    2016-12-19  1.3.dev  (classifiers -> 3.5) requirements-py3  tests/requirements-py3
  2456  7fc11c13    2017-04-28  1.4.dev  (classifiers -> 3.6) requirements-py3  tests/requirements-py3
  2923  b8fabeed    2017-09-04  1.5.dev  (classifiers -> 3.6) requirements-py3  tests/requirements-py3
  2061  094dde6f    2018-12-28  1.6.dev  >=2.7, not 3.0-3.3   requirements-py3  tests/requirements-py3
  3520  8583c033    2019-03-23  1.6.dev  >=2.7, not 3.0-3.3   requirements-py3  tests/requirements-py3
  3563  c81d120b    2019-06-25  1.7.dev  >=2.7, not 3.0-3.3   requirements-py3  tests/requirements-py3
  3858  c57512fa    2020-03-04  2.0.dev  >=3.5                (empty)           tests/requirements-py3
  3608  5d541731    2020-06-18  2.2.dev  >=3.5.2              (empty)           tests/requirements-py3
  3559  68379197    2021-04-26  2.5.dev  >=3.6                (empty)           tests/requirements-py3
  3696  de0e2ccd    2022-06-12  2.6.dev  >=3.7                (absent)          tests/requirements

Five things are worth knowing before changing anything here.

1. PYTHON 3.8, AND IT IS THE ONLY VERSION THAT SPANS THE RANGE. The newest commit sets
   `python_requires='>=3.7'`, so 3.6 and below are out at that end. The six oldest commits all
   do `from collections import MutableMapping` (scrapy/item.py, scrapy/settings/__init__.py,
   and from 2017 on scrapy/utils/datatypes.py too), an alias Python removed in 3.10, so 3.10
   and above are out at the other end. 3.8 sits inside both bounds, and the bookworm tag of
   the 3.8 image still has live apt mirrors. The interpreter's Debian base is not a free
   choice: the python:3.6 images are stretch and buster, both long archived, and the bullseye
   tag of the 3.8 image failed here too on 2026-09-07 - deb.debian.org still serves the
   bullseye-security InRelease index, so `apt-get update` succeeds, but every .deb under
   debian-security/pool returns 404 and the install dies with exit 100. bookworm is the
   oldest suite whose pool is still on the live mirrors.

   The classifier lists on the 2016-2017 commits stop at 3.5 and 3.6, but a classifier records
   what the project tested, not what the code can run, and nothing in those trees is
   3.8-incompatible: there is no `async` used as an identifier and no `time.clock` call
   anywhere under scrapy/ or tests/ at any of the ten commits.

2. ONE BASE IMAGE SERVES ALL TEN. That is only possible because the base stops at `git clone`:
   it installs the build headers and clones, and nothing else. Every dependency decision is a
   fact about one commit, so all of it lives in prepare.sh, which reads the checked-out tree.
   Nothing below branches on a PR number or a sha.

3. THE DEPENDENCY VERSIONS IN THE TREE ARE FLOORS, NOT PINS, so left alone pip resolves them
   against TODAY's PyPI and hands a 2016 tree releases written a decade later. Four have to be
   capped and the rest do not:

     Twisted<22.0.0      22.1 dropped twisted.web.client.HTTPClientFactory, which
                         scrapy/core/downloader/handlers/http10.py imports at every commit here.
     pyOpenSSL==20.0.1   21.0 removed the NPN callback path scrapy/core/downloader/tls.py uses
                         on the older commits.
     cryptography==3.4.8 the last release pyOpenSSL 20.0.1 accepts, and the last with a
                         manylinux wheel that does not need a Rust toolchain to build.
     w3lib<2             2.0 deleted the legacy w3lib.html helpers the 2016-2019 trees call.
     service_identity<22 22.0 imports `asn1` from cryptography.hazmat, which does not exist
                         in 3.4.8. scrapy catches the ImportError and only warns, so the
                         image still builds - and then every TLS test fails with no hostname
                         verification and nothing pointing at this as the cause.

   All five satisfy every floor in the range at once, which is what lets one constraint set
   serve ten commits. The strictest floors anywhere here are Twisted>=18.9.0, cryptography>=2.8,
   pyOpenSSL>=19.1.0 and w3lib>=1.17.0 at the 2022 commit and service_identity>=16.0.0 at the
   2020 ones, and every one of them sits below the caps. They
   are applied AFTER the tree's own requirements so they win the resolution rather than being
   overwritten by it.

4. TEST REQUIREMENTS ARE INSTALLED ONE LINE AT A TIME, and that is deliberate. pip resolves a
   whole requirements file before it installs any of it, so a single unresolvable entry takes
   the entire file down with it - and these files carry several. `leveldb` needs headers no
   base image ships, `mitmproxy` drags in a pinned h2 that fights Twisted, and the 2016-2019
   files pin `pytest==2.9.2` or `pytest==3.6.3`, neither of which installs on 3.8. Per line
   with `|| true`, the entries that matter (testfixtures, jmespath, attrs, sybil, pyftpdlib)
   land regardless, and the runner pins two steps later overwrite whatever pytest the file
   managed to leave behind.

5. GRADED BY `pytest tests`, WITH `--continue-on-collection-errors`. All ten test patches
   write under tests/ - two of them also touch scrapy/utils/test.py and
   scrapy/utils/testsite.py, which are helpers the tests import rather than test modules - so
   `tests` is the smallest target that observes every patch. The flag is not optional: pytest
   ABORTS the whole session when any module fails to import, and the optional extras step 3
   deliberately lets fail are imported by tests/test_proxy_connect.py and tests/test_squeues.py.
   Without it one missing optional dependency reports the entire suite as zero tests, which
   Report.check() rejects while saying nothing about the cause.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# The one version this file fixes. See point 1 for the two bounds that leave 3.8 as the only
# choice, and for why the tag is bullseye rather than a 3.6 image.
PYTHON_IMAGE = "python:3.8-slim-bookworm"

# The caps from point 3, held as one string so prepare.sh cannot install a partial set. They go
# in after the tree's own requirements, never before.
CONSTRAINTS = (
    "'Twisted<22.0.0' 'pyOpenSSL==20.0.1' 'cryptography==3.4.8' 'w3lib<2'"
    " 'service_identity<22'"
)

# The runner. `pytest<7` because every pytest.ini in this range sets `python_classes=` to an
# empty value and passes `--assert=plain`; 7.x changed how it reads an empty ini value.
#
# The two plugins are capped WITH it, and that is not tidiness. pytest loads every installed
# plugin through its entry point before it collects anything, so a plugin built for pytest 7
# raises at startup and the run reports zero tests with no collection error to explain it -
# which is exactly how the 2021-04 and 2022-06 commits failed on 2026-09-07: their
# tests/requirements list pytest-xdist unpinned, pip took 3.6.1 (pytest>=7.0.0), and the
# downgrade to 6.2.5 two lines later left that plugin unloadable. Whatever pytest this file
# pins, every plugin alongside it has to accept the same version.
#
# Every one of the three carries a FLOOR as well as a ceiling, and the floors are what make
# this step do anything at all. pip leaves a package alone when the installed version already
# satisfies the specifier, so a bare `pytest-cov<4` is a no-op against the `pytest-cov==2.2.1`
# the 2016-2019 files pin - and 2.2.1 registers a `pytest_funcarg__cov` hook that pytest 6
# rejects, so every stage died with `INTERNALERROR> PluginValidationError` after collecting
# its 1458 tests. The same trap is waiting on `pytest==3.6.3` and on pytest-twisted.
RUNNER = "'pytest>=6.2,<7' 'pytest-cov>=3,<4' 'pytest-xdist>=2.5,<3'"

# pytest-twisted is installed only when the checked-out pytest.ini asks for it. The eight
# commits up to 2020-06 set `twisted = 1` and their tests are @inlineCallbacks functions that
# need the plugin; the 2021-04 and 2022-06 commits dropped that line and moved to
# twisted.trial.unittest.TestCase, which pytest runs natively. Installing the plugin there
# anyway puts a reactor under a suite that no longer expects one.
TWISTED_INI_PROBE = "grep -qE '^[[:space:]]*twisted[[:space:]]*=' pytest.ini"

# Imported by the suite at every commit in this range but never listed completely. testfixtures
# and jmespath appear only in the 2016-2019 test files; Pillow is a tox `deps` extra and is in
# no requirements file at all, yet tests/test_pipeline_images.py imports it throughout.
#
# google-cloud-storage is here for the same reason, and it is worth being precise about what
# it does and does not buy. The GCS feed-export tests added by the 2020-06 commit skip on
# `except ImportError` alone and mock `google.cloud.storage.Client`, so having the package
# importable is the whole requirement - no project, no bucket, no network. The GCS *files
# store* tests added by the 2017-09 commit are a different thing entirely: they call
# assert_gcs_environ(), read GCS_TEST_FILE_URI and then write, read and delete a real blob,
# so they skip here whatever is installed. Installing it unconditionally rather than for the
# one commit that benefits keeps this free of per-PR branching; everywhere else it is inert.
EXTRA_TEST_DEPS = "testfixtures jmespath Pillow google-cloud-storage"

# `tests` and not the project's own tox posargs `scrapy tests`: every test patch in this range
# writes under tests/, and the extra target only adds --doctest-modules work over the package.
# See point 5 for why --continue-on-collection-errors is load-bearing.
#
# -rA is the only place a PASSING test is named with its full node id. --tb=no keeps
# tracebacks out of a log that is parsed line by line. -p no:cacheprovider stops .pytest_cache
# appearing in the tree between graded stages.
PYTEST_CMD = (
    "python -m pytest tests -v -rA --tb=no"
    " --continue-on-collection-errors -p no:cacheprovider"
)

# Every graded stage starts with this. The stages are separate processes, so anything set in
# prepare.sh has to be re-established here rather than inherited.
SHELL_ENV = """export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
# The suite starts real servers on loopback and several tests assert on the reactor's own
# timeouts. A proxy in the environment redirects those connections, and they then fail with
# nothing in the output pointing at the proxy as the reason.
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY"""


BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM {image}

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

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    TZ=UTC \
    http_proxy=${{http_proxy}} \
    https_proxy=${{https_proxy}} \
    HTTP_PROXY=${{HTTP_PROXY}} \
    HTTPS_PROXY=${{HTTPS_PROXY}} \
    no_proxy=${{no_proxy}} \
    NO_PROXY=${{NO_PROXY}} \
    SSL_CERT_FILE=${{CA_CERT_PATH}} \
    REQUESTS_CA_BUNDLE=${{CA_CERT_PATH}} \
    CURL_CA_BUNDLE=${{CA_CERT_PATH}}

LABEL org.opencontainers.image.title="{org}/{repo}" \
      org.opencontainers.image.description="{org}/{repo} Docker image" \
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /home/

# The headers are the base's whole job besides the clone. lxml and cryptography ship manylinux
# wheels for 3.8 and normally never touch these, but the 2016-2019 trees float their floors
# low enough that a resolver can still land on a source-only release, and a missing libxslt
# header then surfaces as a compiler error thirty lines into a pip log.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl git pkg-config \
    libxml2-dev libxslt1-dev libssl-dev libffi-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

{fetch}

CMD ["/bin/bash"]
"""


class ScrapyLegacyImageBase(Image):
    def __init__(self, pr: PullRequest, config: Config):
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    @property
    def config(self) -> Config:
        return self._config

    def dependency(self) -> Union[str, "Image"]:
        return PYTHON_IMAGE

    def image_tag(self) -> str:
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # The `# syntax=` directive on BASE_DOCKERFILE's first line is load-bearing, not
        # decoration. DockerfileEnhancer.enhance() returns content untouched when it is already
        # present. Without it the enhancer rewrites the clone line into clone +
        # `git checkout ${BASE_COMMIT}` + the full hardening block, which would pin this shared
        # base to whichever PR built it first and scrub its history to that commit - every
        # other PR's checkout would then fail. Opting out means this file supplies what the
        # enhancer would have added: the ARGs, the proxy and cert env, the OCI labels and the
        # CA symlinks, which the MITM proxy the evaluation harness runs behind requires.
        #
        # Nothing after the clone. No checkout, no pip, no hardening - all per-PR, all in the
        # PR image.
        if self.config.need_clone:
            fetch = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            fetch = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        return BASE_DOCKERFILE.format(
            image=image_name,
            org=self.pr.org,
            repo=self.pr.repo,
            fetch=fetch,
        )


class ScrapyLegacyImageDefault(Image):
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
        return ScrapyLegacyImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        env = SHELL_ENV
        repo = self.pr.repo

        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "check_git_changes.sh",
                """\
#!/bin/bash
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
                f"""\
#!/bin/bash
set -e

{env}

cd /home/{repo}

# No git operations in here. The PR Dockerfile has already run `git reset --hard` and
# `git checkout <base.sha>` before this script, and it runs the hardening block after. This
# call only asserts the tree it was handed is clean.
bash /home/check_git_changes.sh

python -m pip install --upgrade pip setuptools wheel

# 1. Runtime requirements, from the file this commit actually carries. The six commits up to
#    2019-06 keep them in requirements-py3.txt; from 2020-03 that file is empty and the deps
#    have moved into setup.py's install_requires; by 2022-06 the file is gone. `-s` covers all
#    three states in one test, which is why no PR number appears here.
if [ -s requirements-py3.txt ]; then
  echo "prepare: runtime requirements from requirements-py3.txt"
  python -m pip install -r requirements-py3.txt || true
fi

# 2. The package itself. This is what picks up install_requires on the 2020+ commits, where it
#    is the only place the runtime dependencies are written down.
python -m pip install -e .

# 3. Test requirements, from the first file this commit has - tests/requirements-py3.txt for
#    the nine older commits, tests/requirements.txt at 2022-06.
#
#    One line at a time, because pip resolves a whole file before installing any of it and
#    these files carry entries that cannot resolve here at all. See point 4 in the module
#    docstring. As a single invocation, any one of them takes testfixtures and jmespath down
#    with it and the suite then reports zero tests.
#
#    The sed strips carriage returns and trailing ` # comment` text. pip accepts a comment
#    inside a requirements FILE but not in an argument, so `sybil >= 1.3.0  # https://...`
#    reaches it as one requirement string and is rejected as a path that does not exist.
for req in tests/requirements-py3.txt tests/requirements.txt; do
  if [ -s "$req" ]; then
    echo "prepare: test requirements from $req"
    sed -e 's/\\r$//' \\
        -e 's/[[:space:]][[:space:]]*#.*$//' \\
        -e 's/[[:space:]][[:space:]]*$//' "$req" > /tmp/test-requirements.txt
    while IFS= read -r line; do
      case "$line" in
        ''|'#'*) continue ;;
      esac
      python -m pip install "$line" || echo "prepare: optional test dep skipped: $line"
    done < /tmp/test-requirements.txt
    break
  fi
done

# 4. The caps. Last, so they win over whatever floors steps 1-3 resolved upward. See point 3
#    for what each one holds back and why all four satisfy every floor in the range at once.
#
#    A hard gate, unlike the steps above it. If this resolution fails, all three graded stages
#    fail identically and the instance reports (0,0,0) three times, which Report.check()
#    rejects without naming a cause. Fail here instead, where the pip error is still on screen.
echo "prepare: applying dependency caps"
python -m pip install {CONSTRAINTS}

# 5. The runner, after step 3 so it overwrites whatever pytest that file pinned.
python -m pip install {RUNNER}

# pytest-twisted only where the checked-out ini asks for it - `twisted = 1` is present through
# 2020-06 and gone from 2021-04 on. Read from the tree, so a commit added to this range gets
# the right answer with no edit here.
if {TWISTED_INI_PROBE}; then
  echo "prepare: pytest.ini declares twisted, installing pytest-twisted"
  python -m pip install 'pytest-twisted>=1.13,<1.14'
fi

python -m pip install {EXTRA_TEST_DEPS} || true

# Re-assert the caps. google-cloud-storage above drags in google-auth and its transitive
# graph, and a resolver free to move cryptography or pyOpenSSL while satisfying it would
# undo step 4 silently - the image would still build and only the TLS tests would tell,
# stage by stage. Running the same pins again costs nothing when nothing moved.
python -m pip install {CONSTRAINTS}

# Two gates, both cheap, both catching a failure that would otherwise reach gen_report as an
# unexplained empty result.
python -c "import scrapy; print('prepare: scrapy', scrapy.__version__)"

python -m pytest tests --collect-only -q > /tmp/collect.log 2>&1 || true
COLLECTED=$(grep -cE '::' /tmp/collect.log || true)
echo "prepare: collected $COLLECTED test ids"
if [ "$COLLECTED" -lt 1 ]; then
  # Print what pytest actually said. A plugin that fails at its entry point aborts the run
  # before collection starts, so there is no collection error in the output and the count
  # alone names nothing - the reason is only ever in this text.
  echo "prepare: pytest collected no tests from tests/, output follows:" >&2
  tail -40 /tmp/collect.log >&2
  exit 1
fi

# A count alone is not enough, because pytest prints the node ids BEFORE it validates the
# plugin set: an incompatible plugin raises in check_pending() after collection has already
# listed every test, so the log shows 1458 ids and then INTERNALERROR, and a gate that only
# counted would pass an image where all three stages run nothing.
if grep -q 'INTERNALERROR' /tmp/collect.log; then
  echo "prepare: pytest raised INTERNALERROR despite collecting, output follows:" >&2
  tail -40 /tmp/collect.log >&2
  exit 1
fi
""",
            ),
            File(
                ".",
                "run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "test-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{PYTEST_CMD}
""",
            ),
            File(
                ".",
                "fix-run.sh",
                f"""\
#!/bin/bash
set -uo pipefail

{env}

cd /home/{repo}
if ! git apply --whitespace=nowarn /home/test.patch /home/fix.patch; then
    echo "Error: git apply failed" >&2
    exit 1
fi
{PYTEST_CMD}
""",
            ),
        ]

    def dockerfile(self) -> str:
        dep = self.dependency()

        # COPY lines are generated from files(), never hand-listed, so a file added there
        # cannot be left uncopied - which would surface at build time as
        # `bash: /home/<x>: No such file or directory`.
        copy_commands = "".join(f"COPY {f.name} /home/\n" for f in self.files())

        # The hardening block with the sha inlined rather than left as ${BASE_COMMIT}, so the
        # generated Dockerfile is self-describing and cannot drift from the sha checked out
        # three lines above it.
        #
        # Order matters: checkout -> prepare.sh -> hardening. prepare.sh builds against the
        # base commit, and the hardening then re-detaches at that same sha, deletes every other
        # ref, expires the reflog and asserts the result - so nothing later than the base
        # commit is reachable from inside the image and a graded stage cannot read the real fix
        # out of git history.
        hardening = Image._HARDENING_BLOCK.replace("${BASE_COMMIT}", self.pr.base.sha)

        return f"""FROM {dep.image_name()}:{dep.image_tag()}

WORKDIR /home/{self.pr.repo}

RUN git reset --hard
RUN git checkout {self.pr.base.sha}

{copy_commands}
RUN bash /home/prepare.sh

{hardening}"""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# pytest's `-rA` short-summary block - the authoritative source, and the only place a PASSING
# test is named with its full node id.
#
#   PASSED tests/test_feedexport.py::FeedExportTest::test_export_no_items_store_empty
#   FAILED tests/test_feedexport.py::FeedExportTest::test_export_multiple_configs - KeyError
#
# The trailing ` - <reason>` is stripped: the reason text differs between stages (different
# assertion values), and a name carrying it would read as two different tests and manufacture
# transitions that never happened.
_SUMMARY_RE = re.compile(
    r"^(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+"
    r"(?P<name>[^\s]+?)(?:\s+-\s+.*)?$"
)

# pytest's `-v` progress line, the fallback when a stage dies before the summary block.
#
#   tests/test_feedexport.py::FeedExportTest::test_export PASSED [ 42%]
#
# The percentage is outside the capture on purpose - it shifts with the size of the suite,
# which is exactly what changes between the run and fix stages.
_PROGRESS_RE = re.compile(
    r"^(?P<name>[^\s]+::[^\s]+)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\b"
)

_FAIL_STATUSES = {"FAILED", "ERROR"}
_SKIP_STATUSES = {"SKIPPED", "XFAIL", "XPASS"}


def parse_pytest_log(test_log: str) -> TestResult:
    """Read a scrapy pytest run into the three buckets report.py compares between stages.

    Both the `-rA` summary and the `-v` progress lines are read and unioned. They name a test
    identically, so a test present in both contributes one entry; reading both means a stage
    killed before it could print the summary still reports what it got through.
    """
    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    # Strip ANSI FIRST. pytest colourises whenever it believes it is attached to a terminal,
    # and a single escape sequence in front of PASSED defeats every pattern below - the stage
    # then reports 0/0/0 and Report.check() rejects it at rule 1 with no clue why.
    for raw_line in _ANSI_RE.sub("", test_log).splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = _SUMMARY_RE.match(line) or _PROGRESS_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        # A summary line for a collection error names a FILE, not a node id. Keeping it is
        # correct - a test module that fails to import is a real regression, and
        # --continue-on-collection-errors means the run reaches the summary to report it - but
        # a bare word off some other log line is not, so require a path or a node id.
        if "::" not in name and "/" not in name:
            continue

        status = match.group("status")
        # Worst result wins, applied as we go rather than as a post-pass, so a test reported
        # twice can never end up in two buckets at once. TestResult.__post_init__ raises
        # ValueError on any overlap, which would abort the whole instance rather than mis-grade
        # it.
        if status in _FAIL_STATUSES:
            passed_tests.discard(name)
            skipped_tests.discard(name)
            failed_tests.add(name)
        elif name not in failed_tests:
            if status in _SKIP_STATUSES:
                if name not in passed_tests:
                    skipped_tests.add(name)
            else:
                skipped_tests.discard(name)
                passed_tests.add(name)

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


@Instance.register("scrapy", "scrapy_3858_to_2061")
class SCRAPY_3858_TO_2061(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return ScrapyLegacyImageDefault(self.pr, self._config)

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
        return parse_pytest_log(test_log)
