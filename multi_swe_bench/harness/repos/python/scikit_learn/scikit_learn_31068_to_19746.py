"""scikit-learn meson era (1.5/1.6/1.8) -- base-31068_to_19746.

Serves pr-28351 (1.5.dev0), pr-19746 and pr-29451 (both 1.6.dev0) and pr-31068
(1.8.dev0). Four PRs, three upstream minor versions, ONE base: every tree here
is built by meson-python against the numpy 2 ABI, and their declared floors do
not conflict, so splitting them would build the same image more than once.

    1.5.dev0   requires-python >=3.9    meson-python>=0.15.0  numpy>=1.25  scipy>=1.6.0
    1.6.dev0   requires-python >=3.9    meson-python>=0.16.0  numpy>=2     scipy>=1.6.0
    1.8.dev0   requires-python >=3.10   meson-python>=0.17.1  numpy>=2     scipy>=1.8.0

python:3.12 and the pins below satisfy the strictest of each column. NOTE the
1.8 meson-python floor above is read from sklearn/_min_dependencies.py, which
is what sklearn/meson.build:55 checks at build time -- pyproject.toml claims
>=0.16.0 there and is stale; building against 0.16.0 fails outright. 1.5 is
included on the evidence of its own changelog -- doc/whats_new/v1.5.rst at
pr-28351's base commit states "This release only includes support for numpy 2"
-- and its classifiers list 3.9 through 3.12, so the numpy 2 / py3.12 stack is
one it declares support for rather than one merely assumed to work.

1.5 is the crossover commit in one respect: setup.py still EXISTS in that tree,
while 1.6 and 1.8 have deleted it. That difference does not matter here, because
all three declare build-backend = "mesonpy" and none of them may be built with
`setup.py build_ext`; the inherited prepare.sh has that step removed below.

pr-19746 is the reason this whole set is routed per PR rather than by a span: it
was opened in March 2021 but merged in October 2024, so its base commit is a 1.6
tree even though its number sits between two 1.0-era PRs. Its base range
therefore OVERLAPS base-22722_to_16449. That overlap is a faithful description
of the data, not a bug; the tag is a label and nothing routes on it.
Instance.create (instance.py:41-51) does an exact dict lookup on
`f"{org}/{number_interval}"` and never compares numbers.

The stock scikit_learn.py supplies files(), the three stage scripts and
parse_log; its prepare.sh is rewritten below to drop the setup.py build step. It
keeps the plain `scikit-learn/scikit-learn` key and is not modified.
"""

from typing import Optional

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance
from multi_swe_bench.harness.pull_request import PullRequest

from multi_swe_bench.harness.repos.python.scikit_learn.scikit_learn import (
    ImageBase as StockImageBase,
    ImageDefault as StockImageDefault,
    SCIKIT_LEARN as StockInstance,
)

PRS = [19746, 28351, 29451, 31068]
BASE_TAG = "base-31068_to_19746"

PIP_ANCHOR = "python --version"

# Installed in prepare.sh rather than the base image, by request. This costs
# one install per PR instead of one per base (QC item A5 calls that pattern
# N-times wasted build time); the pins themselves are unchanged.
PIP_SETUP = """
# gcc 12 (bookworm) hits an internal compiler error in its GIMPLE
# vectorisation pass while compiling the Cython-generated
# _kd_tree.pyx.c for aarch64 ("internal compiler error: in ve..." at
# _kd_tree.cpython-312-aarch64-linux-gnu.so.p/_kd_tree.pyx.c:61092).
# -fno-tree-vectorize disables exactly that pass. It costs some SIMD
# throughput in one extension and changes no semantics, which is the
# right trade for an image whose job is to run tests, not benchmarks.
export CFLAGS="${CFLAGS} -fno-tree-vectorize"
export CXXFLAGS="${CXXFLAGS} -fno-tree-vectorize"

python -m pip install --no-cache-dir "pip==24.2" "setuptools==75.1.0" "wheel==0.44.0" \
    "meson-python==0.17.1" "ninja==1.11.1.1"

python -m pip install --no-cache-dir \
    "numpy==2.1.3" "scipy==1.13.1" "Cython==3.0.11" "joblib==1.4.2" \
    "threadpoolctl==3.5.0" "pandas==2.2.3" "Pillow==11.0.0" "pytest==8.3.4"
"""

# The declared build backend is mesonpy for every tree here, so the inherited
# setup.py build step must not run -- in 1.6/1.8 setup.py does not even exist,
# and in 1.5 it exists but is not the build path. The marker is asserted rather
# than replaced blindly: if the stock prepare.sh changes, this must fail loudly
# instead of silently shipping an image whose extensions were never compiled.
BUILD_MARKER = "python setup.py build_ext --inplace -j 4"
MESON_BUILD = (
    "# meson-python compiles the extensions during the editable install below.\n"
    "# `setup.py build_ext` is not used: the declared build backend is mesonpy\n"
    "# (and in the 1.6/1.8 trees setup.py has been removed entirely)."
)

class ImageBase(StockImageBase):
    def dependency(self) -> str | Image:
        return "python:3.12-slim-bookworm"

    def image_tag(self) -> str:
        return BASE_TAG

    def workdir(self) -> str:
        return BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            clone = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            clone = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        # Rationale for the pins emitted below (kept OUT of the
        # Dockerfile itself, which ships comment-free):
        #   Kept OUT of the canonical ENV block above so that block stays identical
        #   across every repo config. These are stack-specific: pip noise suppression,
        #   and single-threaded BLAS so the graded test run is deterministic.
        #   meson-python 0.17.1: the floor that is ENFORCED comes from
        #   sklearn/_min_dependencies.py (meson.build:55 queries it at build time), NOT
        #   from pyproject.toml. For 1.8 those disagree -- pyproject says >=0.16.0 while
        #   _min_dependencies says >=0.17.1 -- and the build fails on the latter. 0.17.1
        #   also clears 1.5's >=0.15.0 and 1.6's >=0.16.0.
        #   py3.12 clears 1.8's requires-python >=3.10 while staying inside every tree's
        #   classifiers (1.5 and 1.6 list 3.9-3.12, 1.8 lists 3.10-3.13). ninja is the
        #   meson backend generator and must be present before the editable install runs
        #   with --no-build-isolation.
        #   [build-system].requires reads `numpy>=2` for 1.6/1.8 and `numpy>=1.25` for
        #   scipy is pinned to 1.13.1, NOT a later release: scipy >=1.14 segfaults inside
#   scipy.linalg.solve on this stack (reproduced deterministically on
#   test_ridge.py::test_primal_dual_relationship and on IterativeImputer via
#   BayesianRidge; 1.14.1 and 1.15.3 both crash, 1.13.1 passes 834 tests).
#   1.13.1 clears every declared floor here (1.5/1.6 need >=1.6.0, 1.8 >=1.8.0).
#   1.5, and 1.5's own changelog states it "only includes support for numpy 2", so
        #   numpy 2.1.3 is correct for all four PRs. scipy 1.14.1 clears the >=1.6.0 and
        #   >=1.8.0 floors. Every version ships a cp312 manylinux aarch64 wheel, so only
        #   scikit-learn is compiled on the arm64 leg.
        return f"""# syntax=docker/dockerfile:1.6

FROM {image_name}

{self.global_env}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"
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

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PIP_ROOT_USER_ACTION=ignore \\
    OMP_NUM_THREADS=1 \\
    OPENBLAS_NUM_THREADS=1 \\
    MKL_NUM_THREADS=1

LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cacert.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem && \\
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-bundle.crt

WORKDIR /home/

RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

{clone}

WORKDIR /home/{self.pr.repo}

{self.clear_env}

CMD ["/bin/bash"]
"""

class ImageDefault(StockImageDefault):
    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def files(self) -> list[File]:
        rewritten: list[File] = []
        for f in super().files():
            if f.name != "prepare.sh":
                rewritten.append(f)
                continue
            c = f.content
            if PIP_ANCHOR not in c:
                raise RuntimeError(
                    f"{BASE_TAG}: anchor {PIP_ANCHOR!r} not found in the "
                    "inherited prepare.sh; the dependency install has nowhere to go."
                )
            c = c.replace(PIP_ANCHOR, PIP_ANCHOR + "\n" + PIP_SETUP, 1)
            if BUILD_MARKER not in c:
                raise RuntimeError(
                    f"{BASE_TAG}: expected build marker {BUILD_MARKER!r} in "
                    "the inherited prepare.sh and did not find it."
                )
            c = c.replace(BUILD_MARKER, MESON_BUILD)
            rewritten.append(File(f.dir, f.name, c))
        return rewritten

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()
        sha = self.pr.base.sha

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        return f"""FROM {name}:{tag}

{self.global_env}

{copy_commands}
WORKDIR /home/{self.pr.repo}

RUN set -eux; \\
    git checkout --detach {sha}; \\
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
    test "$(git rev-parse HEAD)" = "$(git rev-parse {sha})"; \\
    test -z "$(git for-each-ref refs/heads refs/remotes refs/tags refs/replace)"; \\
    test -z "$(git remote)"; \\
    test "$(git rev-list --all --count)" = "$(git rev-list HEAD --count)"

RUN if [ -f .gitmodules ]; then \\
        git submodule foreach --recursive ' \\
            git checkout --detach HEAD; \\
            git remote remove origin 2>/dev/null || true; \\
            git for-each-ref --format="%(refname)" refs/heads refs/remotes refs/tags refs/replace \\
                | xargs -r -n1 git update-ref -d; \\
            git reflog expire --expire=now --all; \\
            git reflog expire --expire-unreachable=now --all; \\
            git gc --prune=now --aggressive; \\
            rm -f .git/objects/info/alternates; \\
        '; \\
    fi

RUN bash /home/prepare.sh

{self.clear_env}

"""

@Instance.register("scikit-learn", "scikit_learn_19746_to_19746")
@Instance.register("scikit-learn", "scikit_learn_28351_to_28351")
@Instance.register("scikit-learn", "scikit_learn_29451_to_29451")
@Instance.register("scikit-learn", "scikit_learn_31068_to_31068")
class SCIKIT_LEARN_31068_TO_19746(StockInstance):
    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)
