import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ---------------------------------------------------------------------------
# pymc-devs/pymc -- the "pymc/" package era (Oct 2021 - Jan 2022).
#
# Four PRs: 5386, 5159, 5095, 5087. All four sit on `main` AFTER the
# pymc3 -> pymc rename, and all four are Aesara-backed (never Theano, never
# PyTensor). PR 4723 is deliberately NOT here: it predates the rename, still
# ships a `pymc3/` package and lives on the deleted `v4` branch, so it gets its
# own era file (pymc_4723_to_4723.py).
#
# Ancestry, measured against the pin (rule 7 builds 5386 first, so the shared
# base is pinned to fa233aa7):
#
#     b8e1dbe8 (5087)  ancestor of fa233aa7  -> survives the prune
#     f26845de (5095)  ancestor of fa233aa7  -> survives the prune
#     c0c5a808 (5159)  ancestor of fa233aa7  -> survives the prune
#
# So no PR in this era actually needs the fetch-back in prepare.sh. It is kept
# anyway: it is the rule 8 mechanism that lets the base carry the FULL scrub,
# and it costs one no-op `git fetch` when the commit is already present.
# ---------------------------------------------------------------------------

_REPO_DIR = "/home/pymc"
_REPO_URL = "https://github.com/pymc-devs/pymc.git"

# Per-PR backend pins.
#
# requirements.txt at each base commit gives LOWER bounds only (`aesara>=2.2.6`),
# which a modern pip resolves to today's aesara -- a release that postdates these
# trees by years and does not import. The value below is the exact version the
# repo declared at that commit, so it is the version that tree was written
# against. aeppl is listed only where the source actually imports it: at
# f26845de (5095) the only occurrences of the word are four xfail REASON strings
# in test_ndarray_backend.py, so that PR installs no aeppl at all.
_PINS = {
    5087: ["aesara==2.2.6", "aeppl==0.0.13"],
    5095: ["aesara==2.2.2"],
    5159: ["aesara==2.2.6", "aeppl==0.0.15"],
    5386: ["aesara==2.3.6", "aeppl==0.0.18"],
}
_PINS_FALLBACK = ["aesara<2.4"]

# Upper bounds for everything aesara 2.2/2.3 pulls in transitively.
#
# scipy<1.8 is load-bearing, not tidiness: aesara below 2.5 reaches into
# scipy.signal's `_bvalfromboundary`, which scipy 1.8 removed. numpy<1.22 is
# what scipy 1.7.3 was built against. The rest pin arviz and its stack to the
# releases that existed while these PRs were open (Oct 2021 - Jan 2022).
_CONSTRAINTS_COMMON = """\
numpy<1.22
scipy<1.8
pandas<1.4
xarray<0.21
arviz<0.12
matplotlib<3.6
netcdf4<1.6
"""

# The integrity guard prepare.sh calls around every checkout (rule 8).
#
# `git update-index --really-refresh` is NOT decoration. git status compares the
# index's cached size/mtime/dev/ino first and only reads content when that stat
# differs, so a build-time check can pass on a file whose bytes do not match its
# blob -- and then FAIL in the delivered image, because Docker layering changes
# dev/ino and forces the content compare git skipped during the build.
_CHECK_GIT_CHANGES = """\
#!/bin/bash
set -e

if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "check_git_changes: Not inside a git repository"
  exit 1
fi

git update-index -q --really-refresh || true

if [[ -n $(git status --porcelain) ]]; then
  echo "check_git_changes: Uncommitted changes"
  git status --porcelain | head -20
  exit 1
fi

echo "check_git_changes: No uncommitted changes"
exit 0
"""

# Which test files this stage should run, derived from the test patch itself.
#
# Read off the `+++ b/` lines, NOT the `diff --git` header, and that distinction
# is load-bearing. PR 5159's test patch DELETES pymc/tests/test_moment.py, whose
# header still names the path on both sides; the `+++` line for a deletion is
# `+++ /dev/null`, so this form drops it and the header form does not. Getting
# that wrong makes the baseline stage run a file the other two stages no longer
# have, which credits a set of tests that then silently vanish.
#
# `/tests/` additionally drops PR 5087's `.github/workflows/pytest.yml`, which is
# in the test patch but is not a test file.
#
# The `-f` existence check is what makes the BASELINE stage honest: 5087's
# pymc/tests/test_distributions_moments.py does not exist at b8e1dbe8 -- the test
# patch creates it -- so run.sh must find nothing and say so, which is exactly
# how those tests end up classified as n2p rather than f2p.
_DERIVE_TESTS = """\
TEST_FILES="$(grep -E '^\\+\\+\\+ b/[^ ]*/tests/[^ ]*\\.py' /home/test.patch \\
    | sed -E 's#^\\+\\+\\+ b/##; s#[[:space:]].*$##' | sort -u || true)"

SEL=""
for _f in $TEST_FILES; do
    if [ -f "$_f" ]; then SEL="$SEL $_f"; fi
done
SEL="${SEL# }"
"""

# Run pytest, but never let the log go quiet for more than a minute.
#
# docker_util.py builds its client with `docker.from_env(timeout=600)` and reads
# the container with `container.logs(stream=True, follow=True)`, so 600 seconds
# of silence on stdout kills the read -- and pymc has individual sampling tests
# that run for a long time. Writing pytest's output to a file and printing a
# heartbeat beside it keeps the socket alive without touching the results, and
# the whole file is echoed at the end so the parser still sees every line.
#
# The redirect is also why there is no pipe here: `pytest | tee` would hand the
# stage `tee`'s exit status instead of pytest's.
_RUN_PYTEST = """\
if [ -z "$SEL" ]; then
    echo "NO_TEST_FILES_AT_THIS_STAGE"
    exit 0
fi

echo "SELECTED_TEST_FILES: $SEL"

_LOG=/tmp/pytest-stage.log
: > "$_LOG"

timeout -k 60 7200 python -u -m pytest \\
    -rA -v --no-header --color=no -p no:cacheprovider \\
    $SEL > "$_LOG" 2>&1 &
_PID=$!

while kill -0 "$_PID" 2>/dev/null; do
    echo "HEARTBEAT pytest still running"
    sleep 60
done

wait "$_PID" || true
cat "$_LOG"
"""

_APPLY_TEST_PATCH = """\
git apply --whitespace=nowarn /home/test.patch \\
    || git apply --whitespace=nowarn --reject /home/test.patch \\
    || true
find . -name '*.rej' -delete 2>/dev/null || true
"""

_APPLY_FIX_PATCH = """\
git apply --whitespace=nowarn /home/fix.patch \\
    || git apply --whitespace=nowarn --reject /home/fix.patch \\
    || true
find . -name '*.rej' -delete 2>/dev/null || true
"""


def _pins_for(pr: PullRequest) -> str:
    lines = _PINS.get(pr.number, _PINS_FALLBACK)
    return "".join(line + "\n" for line in lines)


def _script(body: str, pr: PullRequest) -> str:
    """Fill the @@...@@ placeholders.

    Placeholders rather than str.format() or an f-string on purpose: these bodies
    are full of ${VAR}, $(...) and find's {} , every one of which would otherwise
    have to be brace-doubled.
    """
    return (
        body.replace("@@REPO_DIR@@", _REPO_DIR)
        .replace("@@REPO_URL@@", _REPO_URL)
        .replace("@@SHA@@", pr.base.sha)
        .replace("@@CONSTRAINTS@@", _CONSTRAINTS_COMMON + _pins_for(pr))
        .replace("@@DERIVE_TESTS@@", _DERIVE_TESTS)
        .replace("@@RUN_PYTEST@@", _RUN_PYTEST)
        .replace("@@APPLY_TEST_PATCH@@", _APPLY_TEST_PATCH)
        .replace("@@APPLY_FIX_PATCH@@", _APPLY_FIX_PATCH)
    )


# A pytest node id, and nothing else.
#
# `-rA` prints the captured log of PASSING tests too, so a suite that logs at
# ERROR level on purpose emits lines like
#     ERROR    pymc.sampling:sampling.py:30 ...
# which a naive `^(PASSED|FAILED|ERROR|SKIPPED)\\s+(\\S+)` reads as a failing test
# named after a LOGGER. A real node id ends in `.py` or `.py::something`; a
# logger name ends in `:<line>`. Requiring the shape separates them, and it also
# drops `SKIPPED [1] path.py:12: reason`, whose second field is `[1]`.
_NODE_ID_RE = re.compile(r"^[^\s:]+\.py(::\S*)?$")

_VERBOSE_RE = re.compile(
    r"^(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_SUMMARY_RE = re.compile(
    r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+)"
)


def parse_pytest(test_log: str) -> TestResult:
    """Parse pytest -rA -v output.

    Two line shapes, because pytest reports the same result twice: once as a
    progress line while running under -v, and once in the -rA short summary.

        pymc/tests/test_types.py::TestType::test_float64 PASSED   [ 25%]
        FAILED pymc/tests/test_types.py::TestType::test_float32 - AssertionError

    ERROR folds into failed -- a collection or fixture error means the test did
    not pass, and a bare `path.py` id with no `::` is exactly how pytest reports
    a module that failed to import, which is a real failure of every test in it.
    XFAIL and XPASS fold into skipped: neither is a result the f2p/p2p
    classifier should credit.
    """
    # ANSI first: pytest colourises the status word, and an invisible escape in
    # front of it stops every pattern below from matching.
    clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

    passed_tests: set[str] = set()
    failed_tests: set[str] = set()
    skipped_tests: set[str] = set()

    for line in clean_log.splitlines():
        line = line.strip()

        name = status = None
        m = _VERBOSE_RE.match(line)
        if m:
            name, status = m.group(1), m.group(2)
        else:
            m = _SUMMARY_RE.match(line)
            if m:
                status, name = m.group(1), m.group(2)

        if not name or not _NODE_ID_RE.match(name):
            continue

        if status == "PASSED":
            passed_tests.add(name)
        elif status in ("FAILED", "ERROR"):
            failed_tests.add(name)
        else:
            skipped_tests.add(name)

    # Keep the three sets disjoint, so an id reported by both the progress line
    # and the summary can never be counted twice. Failure wins over pass, and
    # pass wins over skip.
    passed_tests -= failed_tests
    skipped_tests -= failed_tests
    skipped_tests -= passed_tests

    return TestResult(
        passed_count=len(passed_tests),
        failed_count=len(failed_tests),
        skipped_count=len(skipped_tests),
        passed_tests=passed_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
    )


class PymcImageBase(Image):
    """Shared era base. Owns the toolchain, the clone, the pin to BASE_COMMIT and
    the FULL history scrub (rule 8)."""

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
        # 3.8 because that is the interpreter this era's CI actually ran:
        # conda-envs/environment-dev-py38.yml pins `python=3.8` at every one of
        # the four base commits.
        return "python:3.8-slim"

    def image_tag(self) -> str:
        return "base-pymc_5386_to_5087"

    def workdir(self) -> str:
        return "base-pymc_5386_to_5087"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = 'RUN git clone "${REPO_URL}" /home/%s' % self.pr.repo
        else:
            code = "COPY %s /home/%s" % (self.pr.repo, self.pr.repo)

        label = (
            'LABEL org.opencontainers.image.title="%s/%s" \\\n'
            '      org.opencontainers.image.description="%s/%s Docker image" \\\n'
            '      org.opencontainers.image.source="https://github.com/%s/%s" \\\n'
            '      org.opencontainers.image.authors="https://www.ethara.ai/"'
        ) % (
            self.pr.org, self.pr.repo,
            self.pr.org, self.pr.repo,
            self.pr.org, self.pr.repo,
        )

        # g++ and gfortran are not optional extras here. Aesara compiles a C
        # extension for every graph it evaluates, at RUN time, inside this
        # image -- there is no wheel that can supply that. libopenblas/liblapack
        # give it a BLAS to link against; without one aesara falls back to
        # numpy.distutils detection, which is both slow and broken under the
        # setuptools this era needs.
        toolchain = (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates curl \\\n"
            "        g++ gfortran pkg-config \\\n"
            "        libopenblas-dev liblapack-dev \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )

        sections = [
            "# syntax=docker/dockerfile:1.6",
            "FROM %s" % image_name,
            # BASE_COMMIT pins this shared base. Which commit that is comes from
            # the FIRST row of the routed dataset, which run_pipeline.sh sorts
            # descending (rule 7) so it is the newest -- fa233aa7, PR 5386. Any
            # PR whose commit this prune removes fetches it back in prepare.sh.
            (
                "ARG TARGETARCH\n"
                'ARG REPO_URL="https://github.com/%s/%s.git"\n'
                "ARG BASE_COMMIT"
            ) % (self.pr.org, self.pr.repo),
            # Proxy and CA plumbing. DockerfileEnhancer normally injects this, but
            # enhance() returns a file untouched when the "# syntax=" directive is
            # present -- and that directive is here on purpose. Defaults are empty
            # so no proxy host is ever baked into the image.
            (
                'ARG http_proxy=""\n'
                'ARG https_proxy=""\n'
                'ARG HTTP_PROXY=""\n'
                'ARG HTTPS_PROXY=""\n'
                'ARG no_proxy="localhost,127.0.0.1,::1"\n'
                'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
                'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
            ),
            "ENV DEBIAN_FRONTEND=noninteractive \\\n"
            "    LANG=C.UTF-8 \\\n"
            "    LC_ALL=C.UTF-8 \\\n"
            "    TZ=UTC \\\n"
            "    PYTHONUNBUFFERED=1 \\\n"
            "    PIP_DISABLE_PIP_VERSION_CHECK=1 \\\n"
            "    http_proxy=${http_proxy} \\\n"
            "    https_proxy=${https_proxy} \\\n"
            "    HTTP_PROXY=${HTTP_PROXY} \\\n"
            "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
            "    no_proxy=${no_proxy} \\\n"
            "    NO_PROXY=${NO_PROXY} \\\n"
            "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
            "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
            "    CURL_CA_BUNDLE=${CA_CERT_PATH}",
            label,
            # CA-cert symlink farm. It MUST sit before the first network RUN,
            # because apt, pip and the git clone below are exactly what needs to
            # trust a proxy-injected CA. Different tools look for the bundle at
            # different canonical paths, so the one real file is linked into all.
            (
                "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
                "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
            ),
            "WORKDIR /home/",
            toolchain,
            # Point aesara straight at OpenBLAS and keep every numeric library
            # single-threaded. Oversubscribed BLAS threads inside a container are
            # a classic source of results that change between runs, which would
            # show up here as a rotating f2p set rather than as a slowdown.
            'ENV AESARA_FLAGS="blas__ldflags=-lopenblas"\n'
            "ENV OMP_NUM_THREADS=1\n"
            "ENV OPENBLAS_NUM_THREADS=1\n"
            "ENV MKL_NUM_THREADS=1",
            code,
            "WORKDIR /home/%s" % self.pr.repo,
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            Image._HARDENING_BLOCK.rstrip("\n"),
            'CMD ["/bin/bash"]',
        ]

        blocks = [s for s in sections if s]
        if self.global_env:
            blocks.insert(3, self.global_env)
        if self.clear_env:
            blocks.insert(len(blocks) - 1, self.clear_env)

        return "\n\n".join(blocks) + "\n"


class PymcImageDefault(Image):
    """Per-PR layer. COPY lines and one `RUN bash /home/prepare.sh` (rule 8)."""

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
        return PymcImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return "pr-%d" % self.pr.number

    def workdir(self) -> str:
        return "pr-%d" % self.pr.number

    def files(self) -> list[File]:
        prepare = """#!/bin/bash
set -e

# ------------------------------------------------------- pin to BASE_COMMIT
#
# The shared base image was pruned (git gc) down to a single BASE_COMMIT's
# ancestry by its own hardening block, so THIS PR's commit may be absent.
# Re-attach the remote and fetch the exact sha before the checkout, so one base
# can serve every PR. That is the mechanism that lets the base carry the full
# scrub instead of splitting it.
#
# For this era all three of the other base commits are ancestors of the pin
# (fa233aa7, PR 5386), so the fetch is a verified no-op today. It stays because
# nothing guarantees that for the next dataset built against this file.
#
# The checkout comes BEFORE the dependency install on purpose: every version
# pinned below is read from THIS commit's requirements.txt.
cd @@REPO_DIR@@
git reset --hard
bash /home/check_git_changes.sh
git remote add origin @@REPO_URL@@ 2>/dev/null || true
git fetch --depth=1 origin @@SHA@@ 2>/dev/null || git fetch origin 2>/dev/null || true
git checkout @@SHA@@
bash /home/check_git_changes.sh

# ------------------------------------------------------------- dependencies
#
# setuptools<60 because 60 replaced distutils with its own shim, which breaks
# the numpy.distutils path that aesara and this era's build steps still take.
cat > /home/constraints.txt <<'PYMC_CONSTRAINTS'
@@CONSTRAINTS@@
PYMC_CONSTRAINTS

python -m pip install --no-cache-dir "setuptools<60" "wheel"
python -m pip install --no-cache-dir -c /home/constraints.txt -r requirements.txt
python -m pip install --no-cache-dir -c /home/constraints.txt "pytest<7" "pytest-cov<4"

# Optional at import (pymc guards them with try/except and raises only when the
# distribution is used), but several tests in test_distributions.py do use them.
# Failure here must not fail the build: on a platform with no wheel the honest
# outcome is that those tests fail identically in all three stages, which the
# classifier handles, rather than no image at all.
python -m pip install --no-cache-dir -c /home/constraints.txt polyagamma numdifftools h5py || true

# --no-deps: the pinned set above is the answer. Letting pip re-read
# requirements.txt here would re-resolve aesara to a release from years after
# this tree and undo every pin.
python -m pip install --no-cache-dir --no-deps -e .

# ------------------------------------------------------ compile-cache warm-up
#
# Aesara compiles a C extension per graph on first use and caches it under
# $HOME/.aesara. Doing it once here keeps that cost out of the three timed
# stages, where a cold cache on top of a slow suite is what pushes a run into
# the stage timeout.
python -c "import pymc" || true

# ------------------------------------------------------- back to a clean base
#
# `pip install -e .` writes pymc.egg-info/, and the import above leaves
# __pycache__ behind. Both are covered by .gitignore, so `git clean -fd`
# (no -x) removes stray untracked files without throwing away the install.
git checkout -- .
git clean -fd

# Content compare, not just a status read -- a stat-cache hit can make a dirty
# worktree look clean.
git diff --quiet HEAD
bash /home/check_git_changes.sh
"""

        run = """#!/bin/bash
set -e

cd @@REPO_DIR@@

@@DERIVE_TESTS@@

@@RUN_PYTEST@@
"""

        test_run = """#!/bin/bash
set -e

cd @@REPO_DIR@@

@@APPLY_TEST_PATCH@@

@@DERIVE_TESTS@@

@@RUN_PYTEST@@
"""

        fix_run = """#!/bin/bash
set -e

cd @@REPO_DIR@@

@@APPLY_TEST_PATCH@@
@@APPLY_FIX_PATCH@@

@@DERIVE_TESTS@@

@@RUN_PYTEST@@
"""

        return [
            File(".", "fix.patch", "%s" % self.pr.fix_patch),
            File(".", "test.patch", "%s" % self.pr.test_patch),
            File(".", "check_git_changes.sh", _CHECK_GIT_CHANGES),
            File(".", "prepare.sh", _script(prepare, self.pr)),
            File(".", "run.sh", _script(run, self.pr)),
            File(".", "test-run.sh", _script(test_run, self.pr)),
            File(".", "fix-run.sh", _script(fix_run, self.pr)),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # Rule 8: COPY lines only, one per line, then the single prepare.sh RUN.
        # No ARG, no ENV, no WORKDIR (inherited from the base, which ends in
        # /home/<repo>), no git command, no scrub, no CMD (inherited).
        copy_commands = "\n".join("COPY %s /home/" % f.name for f in self.files())

        return "FROM %s:%s\n\n%s\n\nRUN bash /home/prepare.sh\n" % (
            name,
            tag,
            copy_commands,
        )


@Instance.register("pymc-devs", "pymc_5386_to_5087")
class PYMC_5386_TO_5087(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return PymcImageDefault(self.pr, self._config)

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
        return parse_pytest(test_log)


# Routing aliases for a dataset row whose number_interval carries the bare PR
# number instead of the era key.
#
# Not cosmetic. python/langchain_ai_langgraph/langgraph.py wraps Dataset.build
# GLOBALLY, and its fallback branch runs for every row this harness writes:
#
#     ni = (ds.number_interval or "").strip() or str(pr.number)
#
# run_pipeline.sh stamps the era key, so ds.number_interval is normally
# non-empty and that branch never fires. If it ever does -- an unrouted dataset,
# a direct build_dataset invocation -- Instance.create() looks up
# "pymc-devs/<number>", and without these aliases evaluation dies with
# "Instance 'pymc-devs/5386' is not registered".
#
# Checked before adding: the registry holds four pymc-devs keys
# (pymc_1591_to_1478, pymc_5699_to_5588, pymc_8133_to_4082, pytensor_383_to_232)
# and none is numeric, so nothing is shadowed. Instance.register OVERWRITES
# silently rather than raising, so keep this list to the PRs in this era.
for _pr_number in ("5087", "5095", "5159", "5386"):
    Instance.register("pymc-devs", _pr_number)(PYMC_5386_TO_5087)
