import re
import shlex
from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# Backend pins declared by each PR's own requirements.txt. The era detection in
# extra_setup() infers a RANGE, which contradicts the tree for two PRs:
# 5474 pins aesara==2.3.8 (below the inferred >=2.5) and 6208 pins aesara==2.8.7
# (above the inferred <2.8). aeppl is pinned exactly from 5474 on, while the
# SimplexTransform probe only picks a range. Where the tree states a pin, honour
# it; anything absent here falls through to the inference unchanged.
# 4711 and 4608 are absent deliberately -- both declare lower bounds only.
_EXACT_PINS = {
    # arviz is pinned to 0.11.0, not the tree's own `arviz>=0.9.0`, because
    # pymc3 3.9.3 calls az.geweke at import; arviz dropped it in 0.11.1, so
    # anything newer dies with
    #   AttributeError: module 'arviz' has no attribute 'geweke'
    4207: 'theano-pymc==1.0.11 "numpy<1.22" "scipy>=1.4.1,<1.8" "xarray<2022.6" "arviz==0.11.0" "matplotlib<3.6" "pandas<1.5"',
    5474: 'aesara==2.3.8 aeppl==0.0.26 "scipy>=1.4.1,<1.8.0" "numpy<1.24"',
    5759: 'aesara==2.6.2 aeppl==0.0.28 "scipy<1.12" "numpy<1.24"',
    5763: 'aesara==2.6.2 aeppl==0.0.28 "scipy<1.12" "numpy<1.24"',
    5847: 'aesara==2.6.6 aeppl==0.0.31 "scipy<1.12" "numpy<1.24"',
    6208: 'aesara==2.8.7 aeppl==0.0.38 "scipy<1.12" "numpy<1.24"',
}

# Base commits not reachable from any surviving ref. 4608's sits on the deleted
# `v4` branch, so the template's `git checkout ${BASE_COMMIT}` fails with
# "reference is not a tree" unless the object is fetched by full SHA first.
_UNREACHABLE_BASE = frozenset({4608})

# pytest lives under tests/ (6510+), pymc/tests (5474-6208) or pymc3/tests
# (4207-4711). Verified mutually exclusive at every base commit in this era.
_RESOLVE_TEST_DIR = """if [ -d "tests" ]; then
    TEST_DIR="tests"
elif [ -d "pymc/tests" ]; then
    TEST_DIR="pymc/tests"
elif [ -d "pymc3/tests" ]; then
    TEST_DIR="pymc3/tests"
else
    TEST_DIR="."
fi"""

# --continue-on-collection-errors keeps a module that fails to import from
# aborting the whole run; without it one bad import yields a 0/0/0 TestResult.
#
# The exit-code guard replaces a blanket `|| true`, which would also swallow
# pytest failing to START and hand parse_log an empty log. pytest exits 1 when
# tests fail and 2 on collection errors -- both expected here, since the whole
# point of run.sh and test-run.sh is to capture failures. Anything else (3-5:
# internal error, usage error, no tests collected) is a real problem and must
# stay fatal under `set -eo pipefail`.
_RUN_PYTEST = """set +e
pytest --no-header -rA --tb=no -p no:cacheprovider "$TEST_DIR" --continue-on-collection-errors
PYTEST_RC=$?
set -e
if [ "$PYTEST_RC" -gt 2 ]; then
    echo "pytest failed to run (exit $PYTEST_RC)" >&2
    exit "$PYTEST_RC"
fi"""


# Two bases, split on the interpreter each stack actually supports.
# 4207-5847 pin numpy<1.24 / scipy<1.8 and declare python_requires >=3.6/3.7;
# 3.9 is the newest interpreter with wheels for that whole set. 6208-6897
# declare >=3.8/3.9 and run aesara 2.8 / pytensor, which want a newer one.
_PY39_MAX_PR = 5847


def _base_key(pr: PullRequest) -> str:
    return "py39" if pr.number <= _PY39_MAX_PR else "py310"


def _base_image(pr: PullRequest) -> str:
    return "python:3.9-bookworm" if _base_key(pr) == "py39" else "python:3.10-bookworm"


# Everything the environment needs, as one script the PR Dockerfile calls once.
# It lives here rather than in Dockerfile RUN layers so the PR image carries
# only structure, and -- load-bearing -- so the source patches happen AFTER the
# checkout that the git scrub performs. When the patching ran as a Dockerfile
# layer, prepare.sh's later `git reset --hard` reverted it and every aesara-era
# test aborted at conftest import with
#     TypeError: __init__() got an unexpected keyword argument 'A_structure'
#
# Order matters throughout: detect era -> patch source -> install backend ->
# install project -> post-install fixups -> pytest. Patching must precede the
# install because the installed aesara decides which API the source must use.
_PREPARE = r"""#!/bin/bash
set -e
cd /home/@@REPO@@

# Same reason as the run scripts: without an explicit BLAS the backend probes
# numpy via np.__config__.get_info, removed in numpy 1.26.
export AESARA_FLAGS="blas__ldflags=-lopenblas"
export THEANO_FLAGS="blas__ldflags=-lopenblas"
export PYTENSOR_FLAGS="blas__ldflags=-lopenblas"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1

# The scrub already detached HEAD here; assert rather than re-checkout, and do
# NOT reset -- the patches below are deliberate working-tree edits.
test "$(git rev-parse HEAD)" = "@@SHA@@"

if grep -qi 'pytensor' requirements.txt 2>/dev/null; then
    ERA=pytensor
elif grep -qi 'aesara' requirements.txt 2>/dev/null; then
    ERA=aesara
elif grep -qi 'theano' requirements.txt 2>/dev/null; then
    ERA=theano
else
    ERA=unknown
fi
if [ -d pymc ]; then PKG=pymc; elif [ -d pymc3 ]; then PKG=pymc3; else PKG=pymc; fi
echo "Detected era=$ERA pkg=$PKG"

# Rewrite APIs the installed backend no longer provides.
if [ "$ERA" = "aesara" ]; then
    find "$PKG" -name '*.py' -exec grep -l 'A_structure' {} \; 2>/dev/null | while read -r f; do
        sed -i 's/Solve(A_structure="lower_triangular")/Solve(assume_a="gen", lower=True)/g' "$f"
        sed -i 's/Solve(A_structure="upper_triangular")/Solve(assume_a="gen", lower=False)/g' "$f"
        sed -i "s/Solve(A_structure='lower_triangular')/Solve(assume_a='gen', lower=True)/g" "$f"
        sed -i "s/Solve(A_structure='upper_triangular')/Solve(assume_a='gen', lower=False)/g" "$f"
        sed -i 's/Solve(A_structure="general")/Solve(assume_a="gen")/g' "$f"
        sed -i "s/Solve(A_structure='general')/Solve(assume_a='gen')/g" "$f"
    done
    find "$PKG" -name '*.py' -exec grep -l 'from aesara.graph.type import CType' {} \; 2>/dev/null | while read -r f; do
        sed -i 's/from aesara.graph.type import CType/from aesara.graph.type import Type as CType/g' "$f"
    done
    # aesara renamed default_shape_from_params -> default_supp_shape_from_params
    # in 2.4. Which way to rewrite depends on the version actually installed, so
    # this runs after the backend install; see the post-install fixups.
    # aesara moved sigmoid/softplus out of nnet into tensor proper after 2.1,
    # reached both as an import and as an `at.nnet.X` attribute.
    find "$PKG" -name '*.py' -exec grep -lE 'aesara\.tensor\.nnet|at\.nnet\.' {} \; 2>/dev/null | while read -r f; do
        sed -i 's/from aesara\.tensor\.nnet\.sigm import /from aesara.tensor import /g' "$f"
        sed -i 's/from aesara\.tensor\.nnet import /from aesara.tensor import /g' "$f"
        sed -i 's/\bat\.nnet\.sigmoid\b/at.sigmoid/g' "$f"
        sed -i 's/\bat\.nnet\.softplus\b/at.softplus/g' "$f"
        sed -i 's/\baesara\.tensor\.nnet\.sigmoid\b/aesara.tensor.sigmoid/g' "$f"
        sed -i 's/\baesara\.tensor\.nnet\.softplus\b/aesara.tensor.softplus/g' "$f"
    done
elif [ "$ERA" = "theano" ]; then
    # theano-pymc renamed config sections dotted -> double-underscore in 1.0.12.
    # The tree here predates that, so which way to rewrite depends on which
    # version is actually installed -- an unconditional rewrite to
    # gcc__cxxflags breaks 1.0.11 with
    #   AttributeError: 'TheanoConfigParser' object has no attribute 'gcc__cxxflags'
    # Defer until after the install; see the post-install fixups below.
    :
fi

# Backend install. @@PINS@@ is this PR's own declared versions when it has them
# (see _EXACT_PINS); otherwise fall back to an era-inferred range.
# @@PINS@@ expands to a `set -- 'spec' 'spec' ...` line (or `set --` when this
# PR has no declared pins), each spec single-quoted by the generator. Assigning
# them to a variable first does NOT work: the specs carry their own double
# quotes, so PINS="a==1 "b<2"" terminates the string early and the shell reads
# the bare `<` as a redirection -- the `1.12 numpy: No such file` failure.
@@PINS@@
if [ "$#" -gt 0 ]; then
    pip install --no-cache-dir "$@"
    : > /tmp/constraints.txt
    for spec in "$@"; do printf '%s\n' "$spec" >> /tmp/constraints.txt; done
elif [ "$ERA" = "pytensor" ]; then
    pip install --no-cache-dir "numpy<2" "scipy<1.14"
    printf '%s\n' 'numpy<2' 'scipy<1.14' > /tmp/constraints.txt
elif [ "$ERA" = "aesara" ]; then
    # aesara<2.5 imports scipy.signal._bvalfromboundary, removed in scipy 1.8.
    AESARA_SPEC="aesara>=2.5.0,<2.8"
    pip install --no-cache-dir "numpy<1.24" "scipy<1.12"
    if grep -q 'SimplexTransform' "$PKG/distributions/transforms.py" 2>/dev/null; then
        pip install --no-cache-dir "aeppl>=0.0.28,<0.1" "$AESARA_SPEC" "xarray<2024.1"
        printf '%s\n' 'aeppl>=0.0.28,<0.1' "$AESARA_SPEC" 'numpy<1.24' 'scipy<1.12' 'xarray<2024.1' > /tmp/constraints.txt
    elif [ -f "$PKG/distributions/transforms.py" ]; then
        pip install --no-cache-dir "aeppl>=0.0.6,<0.0.28" "$AESARA_SPEC" "xarray<2024.1"
        printf '%s\n' 'aeppl>=0.0.6,<0.0.28' "$AESARA_SPEC" 'numpy<1.24' 'scipy<1.12' 'xarray<2024.1' > /tmp/constraints.txt
    else
        pip install --no-cache-dir "$AESARA_SPEC" "xarray<2024.1" || true
        printf '%s\n' "$AESARA_SPEC" 'numpy<1.24' 'scipy<1.12' 'xarray<2024.1' > /tmp/constraints.txt
    fi
elif [ "$ERA" = "theano" ]; then
    pip install --no-cache-dir "numpy<1.22" "scipy>=1.4.1,<1.8" "xarray<2022.6" "arviz<0.12" "matplotlib<3.6" "pandas<1.5"
    pip install --no-cache-dir --no-build-isolation "theano-pymc==1.0.12"
    printf '%s\n' 'numpy<1.22' 'scipy<1.8' 'xarray<2022.6' 'arviz<0.12' 'matplotlib<3.6' 'pandas<1.5' > /tmp/constraints.txt
else
    pip install --no-cache-dir "numpy<1.24" "scipy<1.12"
    printf '%s\n' 'numpy<1.24' 'scipy<1.12' > /tmp/constraints.txt
fi

# Version-dependent source patches, now that the backend is installed and can
# be introspected. Must precede the project install, which imports the package.
if [ "$ERA" = "aesara" ]; then
    # Renamed in aesara 2.4: 2.3.x exposes default_shape_from_params, 2.4+
    # default_supp_shape_from_params. Rewriting unconditionally breaks whichever
    # side is not installed, e.g. PR 5474 on aesara 2.3.8 died with
    #   ImportError: cannot import name 'default_supp_shape_from_params'
    if python -c "from aesara.tensor.random.op import default_supp_shape_from_params" 2>/dev/null; then
        FROM_NAME='default_shape_from_params'; TO_NAME='default_supp_shape_from_params'
    else
        FROM_NAME='default_supp_shape_from_params'; TO_NAME='default_shape_from_params'
    fi
    grep -rl "$FROM_NAME" "$PKG" --include='*.py' 2>/dev/null | while read -r f; do
        sed -i "s/\b$FROM_NAME\b/$TO_NAME/g" "$f"
    done
fi

# Install the project itself.
if [ -f pyproject.toml ]; then
    pip install --no-cache-dir -c /tmp/constraints.txt -e ".[dev]" 2>/dev/null || \
    pip install --no-cache-dir -c /tmp/constraints.txt -e . 2>/dev/null || \
    (pip install --no-cache-dir -c /tmp/constraints.txt arviz cachetools cloudpickle fastprogress pandas typing-extensions && \
     pip install --no-cache-dir -c /tmp/constraints.txt -e .) || true
elif [ -f setup.py ]; then
    pip install --no-cache-dir -c /tmp/constraints.txt -r requirements.txt 2>/dev/null || true
    pip install --no-cache-dir -c /tmp/constraints.txt -e . 2>/dev/null || \
    pip install --no-cache-dir -c /tmp/constraints.txt . || true
fi

# requirements.txt above can pull upstream theano over theano-pymc. The
# scan_module symlink restores a path pymc3 imports but theano-pymc renamed.
if [ "$ERA" = "theano" ]; then
    THEANO_SPEC=$(grep -o 'theano-pymc==[0-9.]*' /tmp/constraints.txt | head -1)
    [ -n "$THEANO_SPEC" ] || THEANO_SPEC="theano-pymc==1.0.12"
    pip uninstall -y theano theano-pymc 2>/dev/null || true
    THEANO_DIR=$(python -c "import site; print(site.getsitepackages()[0])")/theano
    rm -rf "$THEANO_DIR"
    pip install --no-cache-dir --no-deps --no-build-isolation "$THEANO_SPEC"
    [ -d "$THEANO_DIR/scan" ] && [ ! -e "$THEANO_DIR/scan_module" ] && \
        ln -s "$THEANO_DIR/scan" "$THEANO_DIR/scan_module" || true
    # Now that theano is installed, rewrite the source to whichever config
    # spelling this version actually exposes.
    if python -c "import theano; theano.config.gcc__cxxflags" 2>/dev/null; then
        sed -i 's/theano\.config\.gcc\.cxxflags/theano.config.gcc__cxxflags/g' "$PKG/__init__.py" 2>/dev/null || true
    else
        sed -i 's/theano\.config\.gcc__cxxflags/theano.config.gcc.cxxflags/g' "$PKG/__init__.py" 2>/dev/null || true
    fi
elif [ "$ERA" = "aesara" ]; then
    python -c "from aesara.graph.type import CType" 2>/dev/null || \
    (AESARA_TYPE=$(python -c "import aesara.graph.type as t; print(t.__file__)") && \
     echo "CType = Type  # compatibility alias" >> "$AESARA_TYPE")
fi

pip install --no-cache-dir -c /tmp/constraints.txt pytest pytest-xdist pytest-cov ipython 2>/dev/null || \
    pip install --no-cache-dir pytest pytest-xdist pytest-cov ipython || true

python -c "import $PKG" 2>&1 | tail -3 || echo "WARNING: $PKG import failed; tests will report the error"
"""


def _pins_set_line(pr: PullRequest) -> str:
    """`set --` line placing this PR's declared pins in "$@", safely quoted.

    shlex.split parses the spec string the way a shell would; shlex.quote then
    re-quotes each element so no '<' or '>' can be read as a redirection. Emits
    a bare `set --` (empty "$@") for a PR with no declared pins, which the
    script's `[ "$#" -gt 0 ]` test routes to the era inference.
    """
    spec = _EXACT_PINS.get(pr.number, "")
    if not spec:
        return "set --"
    return "set -- " + " ".join(shlex.quote(s) for s in shlex.split(spec))


def _exact_pin_setup(pr: PullRequest) -> str:
    """Install layer for a PR with declared pins, or "" to use the inference."""
    spec = _EXACT_PINS.get(pr.number)
    if not spec:
        return ""
    # printf with each constraint as a single-quoted argument, NOT a heredoc.
    # The PR Dockerfile carries no "# syntax=docker/dockerfile:1.6" directive
    # (only the base does), so the classic builder parses it -- and classic has
    # no heredoc support: it reads `RUN cat > f <<'EOF'` as the whole
    # instruction and drops the body, leaving an EMPTY constraints file. That is
    # silent: the pip install on the same layer still succeeds, and only the
    # later `grep theano-pymc== /tmp/constraints.txt` comes back empty.
    # Single quotes keep '<' in specs like scipy<1.12 from being read as a
    # redirection; shlex.split undoes the quoting the pip command line needs so
    # each constraint becomes its own line.
    quoted = " ".join("'%s'" % c for c in shlex.split(spec))
    return (
        "RUN pip install --no-cache-dir %s && \\\n"
        "    printf '%%s\\n' %s > /tmp/constraints.txt && \\\n"
        "    touch /tmp/pymc_pinned\n\n" % (spec, quoted)
    )


class ImageBase(Image):
    """Toolchain + clone only. Shared by every PR on the same interpreter, so
    the ten PR images build on two bases rather than ten."""

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
        return _base_image(self.pr)

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"base-{_base_key(self.pr)}"

    def workdir(self) -> str:
        return f"base-{_base_key(self.pr)}"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        # Written out rather than assembled by the base class because the
        # "# syntax=" directive makes DockerfileEnhancer skip the file, so the
        # ARG/ENV/label/CA plumbing it would otherwise inject must be here.
        org, repo = self.pr.org, self.pr.repo
        return "\n\n".join(
            [
                "# syntax=docker/dockerfile:1.6",
                "FROM %s" % _base_image(self.pr),
                (
                    "ARG TARGETARCH\n"
                    'ARG REPO_URL="https://github.com/%s/%s.git"\n'
                    "ARG BASE_COMMIT"
                ) % (org, repo),
                (
                    'ARG http_proxy=""\n'
                    'ARG https_proxy=""\n'
                    'ARG HTTP_PROXY=""\n'
                    'ARG HTTPS_PROXY=""\n'
                    'ARG no_proxy="localhost,127.0.0.1,::1"\n'
                    'ARG NO_PROXY="localhost,127.0.0.1,::1"\n'
                    'ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"'
                ),
                (
                    "ENV DEBIAN_FRONTEND=noninteractive \\\n"
                    "    LANG=C.UTF-8 \\\n"
                    "    TZ=UTC \\\n"
                    "    http_proxy=${http_proxy} \\\n"
                    "    https_proxy=${https_proxy} \\\n"
                    "    HTTP_PROXY=${HTTP_PROXY} \\\n"
                    "    HTTPS_PROXY=${HTTPS_PROXY} \\\n"
                    "    no_proxy=${no_proxy} \\\n"
                    "    NO_PROXY=${NO_PROXY} \\\n"
                    "    SSL_CERT_FILE=${CA_CERT_PATH} \\\n"
                    "    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \\\n"
                    "    CURL_CA_BUNDLE=${CA_CERT_PATH}"
                ),
                (
                    'LABEL org.opencontainers.image.title="%s/%s" \\\n'
                    '      org.opencontainers.image.description="%s/%s Docker image" \\\n'
                    '      org.opencontainers.image.source="https://github.com/%s/%s" \\\n'
                    '      org.opencontainers.image.authors="https://www.ethara.ai/"'
                ) % (org, repo, org, repo, org, repo),
                (
                    "RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\\n"
                    "    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt"
                ),
                # gfortran/openblas are required: aesara compiles a C extension
                # per graph at run time, and without a BLAS to link against it
                # falls back to numpy.distutils detection.
                (
                    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
                    "    ca-certificates \\\n"
                    "    curl \\\n"
                    "    build-essential \\\n"
                    "    git \\\n"
                    "    gnupg \\\n"
                    "    make \\\n"
                    "    sudo \\\n"
                    "    wget \\\n"
                    "    g++ \\\n"
                    "    gfortran \\\n"
                    "    libopenblas-dev \\\n"
                    "    liblapack-dev \\\n"
                    "    pkg-config \\\n"
                    "    && rm -rf /var/lib/apt/lists/*"
                ),
                'RUN pip install --no-cache-dir --upgrade pip "setuptools<70" wheel',
                # Point the backend at OpenBLAS and hold every numeric library
                # to one thread. Without the caps each pytest process spawns
                # ~10 BLAS threads, and several instances running at once
                # oversubscribe the cores badly -- measured on this dataset as
                # 4 containers x 10 threads, which burns CPU on context
                # switching rather than work. Single-threaded BLAS also keeps
                # results reproducible, which matters here because a rotating
                # f2p set would look like a flaky config rather than a slow one.
                'ENV AESARA_FLAGS="blas__ldflags=-lopenblas"\n'
                'ENV THEANO_FLAGS="blas__ldflags=-lopenblas"\n'
                'ENV PYTENSOR_FLAGS="blas__ldflags=-lopenblas"\n'
                "ENV OMP_NUM_THREADS=1\n"
                "ENV OPENBLAS_NUM_THREADS=1\n"
                "ENV MKL_NUM_THREADS=1",
                "WORKDIR /home/",
                'RUN git clone "${REPO_URL}" /home/%s' % repo,
                "WORKDIR /home/%s" % repo,
                'CMD ["/bin/bash"]',
            ]
        ) + "\n"


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

    def image_prefix(self) -> str:
        return "mswebench"

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def extra_packages(self) -> list[str]:
        return [
            "g++",
            "gfortran",
            "libopenblas-dev",
            "liblapack-dev",
            "pkg-config",
        ]

    def extra_setup(self) -> str:
        # Deliberately empty. Everything this used to emit as Dockerfile RUN
        # layers now lives in prepare.sh, so the PR image carries only
        # structure: FROM, COPY, WORKDIR, the scrub, submodules, prepare.sh.
        return ""

    def files(self) -> list[File]:
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(
                ".",
                "prepare.sh",
                _PREPARE.replace("@@REPO@@", self.pr.repo)
                .replace("@@SHA@@", self.pr.base.sha)
                .replace("@@PINS@@", _pins_set_line(self.pr)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Name the BLAS explicitly. Left unset, pytensor/aesara probe numpy for it via
# np.__config__.get_info("blas_opt"), which numpy 1.26 removed -- so pymc fails
# at import with "module 'numpy.__config__' has no attribute 'get_info'" and the
# whole stage aborts before collection. Single-threaded keeps several instances
# from oversubscribing the cores and keeps results reproducible.
export AESARA_FLAGS="blas__ldflags=-lopenblas"
export THEANO_FLAGS="blas__ldflags=-lopenblas"
export PYTENSOR_FLAGS="blas__ldflags=-lopenblas"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
cd /home/{repo}

{resolve}

{pytest}
""".format(repo=self.pr.repo, resolve=_RESOLVE_TEST_DIR, pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Name the BLAS explicitly. Left unset, pytensor/aesara probe numpy for it via
# np.__config__.get_info("blas_opt"), which numpy 1.26 removed -- so pymc fails
# at import with "module 'numpy.__config__' has no attribute 'get_info'" and the
# whole stage aborts before collection. Single-threaded keeps several instances
# from oversubscribing the cores and keeps results reproducible.
export AESARA_FLAGS="blas__ldflags=-lopenblas"
export THEANO_FLAGS="blas__ldflags=-lopenblas"
export PYTENSOR_FLAGS="blas__ldflags=-lopenblas"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch

{resolve}

{pytest}
""".format(repo=self.pr.repo, resolve=_RESOLVE_TEST_DIR, pytest=_RUN_PYTEST),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true
# Name the BLAS explicitly. Left unset, pytensor/aesara probe numpy for it via
# np.__config__.get_info("blas_opt"), which numpy 1.26 removed -- so pymc fails
# at import with "module 'numpy.__config__' has no attribute 'get_info'" and the
# whole stage aborts before collection. Single-threaded keeps several instances
# from oversubscribing the cores and keeps results reproducible.
export AESARA_FLAGS="blas__ldflags=-lopenblas"
export THEANO_FLAGS="blas__ldflags=-lopenblas"
export PYTENSOR_FLAGS="blas__ldflags=-lopenblas"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch

{resolve}

{pytest}
""".format(repo=self.pr.repo, resolve=_RESOLVE_TEST_DIR, pytest=_RUN_PYTEST),
            ),
        ]

    def dockerfile(self) -> str:
        sha = self.pr.base.sha
        copy_commands = "\n".join(f"COPY {f.name} /home/" for f in self.files())

        sections = [
            "FROM %s" % self.dependency().image_full_name(),
            copy_commands,
            "WORKDIR /home/%s" % self.pr.repo,
        ]

        # GitHub serves an object reachable from no ref only because
        # uploadpack.allowAnySHA1InWant is on, and only for a full 40-char SHA.
        # Not --depth=1: a shallow fetch grafted onto a full clone leaves
        # .git/shallow behind, and the scrub's rev-list assert below would then
        # compare two truncated histories and pass without meaning anything.
        if self.pr.number in _UNREACHABLE_BASE:
            sections.append(
                "RUN git fetch --no-tags origin \\\n"
                '        "+%s:refs/heads/msb-base-commit"' % sha
            )

        # Pins the tree to the base commit and reduces the repository to exactly
        # that history, then asserts: HEAD == base commit, no residual refs, no
        # remotes, no unreachable objects.
        sections.append(
            "RUN set -eux; \\\n"
            "    git checkout --detach %s; \\\n"
            "    git remote remove origin 2>/dev/null || true; \\\n"
            "    git for-each-ref --format='%%(refname)' refs/heads refs/remotes refs/tags refs/replace \\\n"
            "        | xargs -r -n1 git update-ref -d; \\\n"
            "    git reflog expire --expire=now --all; \\\n"
            "    git reflog expire --expire-unreachable=now --all; \\\n"
            "    git gc --prune=now --aggressive; \\\n"
            "    git repack -a -d -l --quiet; \\\n"
            "    rm -f .git/objects/info/alternates; \\\n"
            "    git config --local gc.auto 0; \\\n"
            "    git config --local fetch.recurseSubmodules false; \\\n"
            '    git config --local remote.pushDefault ""; \\\n'
            '    test "$(git rev-parse HEAD)" = "$(git rev-parse %s)"; \\\n'
            "    test -z \"$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)\"; \\\n"
            '    test -z "$(git remote)"; \\\n'
            '    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"'
            % (sha, sha)
        )
        sections.append(
            "RUN if [ -f .gitmodules ]; then \\\n"
            "        git submodule foreach --recursive ' \\\n"
            "            git checkout --detach HEAD; \\\n"
            "            git remote remove origin 2>/dev/null || true; \\\n"
            '            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\\n'
            "                | xargs -r -n1 git update-ref -d; \\\n"
            "            git reflog expire --expire=now --all; \\\n"
            "            git reflog expire --expire-unreachable=now --all; \\\n"
            "            git gc --prune=now --aggressive; \\\n"
            "            rm -f .git/objects/info/alternates; \\\n"
            "        '; \\\n"
            "    fi"
        )
        sections.append("RUN bash /home/prepare.sh")
        sections.append('CMD ["/bin/bash"]')

        return "\n\n".join(s for s in sections if s) + "\n"


@Instance.register("pymc-devs", "pymc_8133_to_4082")
class PYMC_8133_TO_4082(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return ImageDefault(self.pr, self._config)

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

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)

    def parse_log(self, test_log: str) -> TestResult:
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()
        # One status per node id, so the three sets are disjoint by construction.
        test_results: dict[str, str] = {}

        clean_log = self._strip_ansi(test_log)
        # `-rA` also prints the captured logs of passing tests, so a suite that
        # logs at ERROR level emits lines like
        #     ERROR    pymc.sampling:sampling.py:30 ...
        # which differ from a real result line only in what follows the status.
        # Requiring a pytest node id -- a path ending in .py, then ::name --
        # rejects those; anchoring alone does not.
        # A collection error names the module with no ::, so .py may end it.
        #
        # The node id is NOT \S+: pytest renders tuple parameters with a space,
        # e.g. test_with_np_arrays[(10, 3)-(3,)-(1,)-cov]. Truncating at the
        # first space collapses every parametrization of one test into the same
        # id -- on PR 4207 that turned 170 distinct FAILED lines into 39 ids and
        # under-reported the failure count by 131. So take the id up to the
        # " - <message>" separator pytest puts before the reason, or to end of
        # line when there is none.
        pattern = re.compile(
            r"^(PASSED|FAILED|ERROR|SKIPPED(?: \[\d+\])?|XFAIL|XPASS)\s+"
            r"(\S+\.py(?:::.*?)?)(?: - .*)?$"
        )
        for line in clean_log.splitlines():
            match = pattern.match(line)
            if match:
                status, test_name = match.group(1), match.group(2)
                test_name = test_name.strip()
                # Match XFAIL/XPASS BEFORE the plain FAIL/PASS tests: `"FAIL" in
                # "XFAIL"` is True, so an `if "FAIL" in status` placed first
                # swallows every xfail and books it as a real failure. That is
                # not cosmetic -- pymc xfails thousands of tests per run, and
                # the count swings between stages (404 in test vs 1472 in fix
                # for PR 4608), so the two stages get wildly different failure
                # totals for tests that never ran. An xfail is an expected
                # failure and an xpass an unexpected pass; neither is a signal
                # about the fix, so both are recorded as skipped and excluded
                # from f2p/p2p classification.
                if status in ("XFAIL", "XPASS"):
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "FAIL" in status or "ERROR" in status:
                    test_results[test_name] = "failed"
                elif "SKIP" in status:
                    if test_results.get(test_name) != "failed":
                        test_results[test_name] = "skipped"
                elif "PASS" in status:
                    if test_results.get(test_name) not in ["failed", "skipped"]:
                        test_results[test_name] = "passed"

        for test_name, status in test_results.items():
            if status == "passed":
                passed_tests.add(test_name)
            elif status == "failed":
                failed_tests.add(test_name)
            elif status == "skipped":
                skipped_tests.add(test_name)

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# Instance.create() keys off pr.number_interval and falls back to "{org}/{repo}"
# (not "{org}/{number}") when it is empty. The raw dataset carries no
# number_interval and no tag, so every row resolves to "pymc-devs/pymc". Without
# this all ten die with "Instance 'pymc-devs/pymc' is not registered".
# uutils/coreutils registers its bare org/repo key for the same reason.
# Caveat: this makes 4082-8133 the default for any unstamped pymc-devs/pymc row,
# so stamp number_interval on future 4723 or 5087-5386 rows rather than relying
# on the fallback.
Instance.register("pymc-devs", "pymc")(PYMC_8133_TO_4082)
