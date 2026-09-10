"""scikit-learn 1.4.dev0 era -- base-26634_to_26634.

Serves pr-26634 (base 2023-06-23). The LAST era still built by setup.py: 1.5
flips [build-system].build-backend to mesonpy, and 1.6 deletes setup.py outright.

Kept separate from base-22722_to_16449 rather than merged into it: 1.4 raises
Cython to >=0.29.33 and pytest to >=7.1.2, and folding those newer pins back
under pr-16449/pr-20231 would build those PRs against a toolchain their own
setup.py never declared.

ROUTING. Registered under `scikit_learn_26634_to_26634`, one interval per PR,
because a PR's base.sha is taken near its MERGE and so the scikit-learn version
at each base commit is NOT ordered by PR number -- pr-19746 (opened 2021, merged
2024) is a 1.6 tree with no setup.py despite sitting below this PR by number. A
contiguous span key would group PRs whose trees need different toolchains.
Instance.create (instance.py:41-51) does an exact dict lookup on
`f"{org}/{number_interval}"` and never compares numbers. The base TAG is a label
only -- nothing routes on it.

The stock scikit_learn.py supplies files(), the three stage scripts and
parse_log. It keeps the plain `scikit-learn/scikit-learn` key and is not
modified.
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

PRS = [26634]
BASE_TAG = "base-26634_to_26634"

PIP_ANCHOR = "python --version"

# Installed in prepare.sh rather than the base image, by request. This costs
# one install per PR instead of one per base (QC item A5 calls that pattern
# N-times wasted build time); the pins themselves are unchanged.
PIP_SETUP = """
python -m pip install --no-cache-dir "pip==24.0" "setuptools==68.2.2" "wheel==0.42.0"

python -m pip install --no-cache-dir \
    "numpy==1.26.4" "scipy==1.11.4" "Cython==0.29.36" "joblib==1.3.2" \
    "threadpoolctl==3.2.0" "pandas==2.0.3" "Pillow==10.2.0" "pytest==7.4.4"
"""

class ImageBase(StockImageBase):
    def dependency(self) -> str | Image:
        return "python:3.10-slim-bookworm"

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
        #   1.4.dev0 declares Cython>=0.29.33, scipy>=1.5.0, joblib>=1.1.1, pytest>=7.1.2
        #   and python_requires >=3.8. numpy 1.26.4 is the FINAL 1.x release, so
        #   numpy.distutils still exists for this setup.py build; numpy 2 removes it, and
        #   this tree predates scikit-learn's numpy 2 support (added at 1.4.2/1.5.1).
        #   Every version here ships a cp310 manylinux aarch64 wheel.
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

@Instance.register("scikit-learn", "scikit_learn_26634_to_26634")
class SCIKIT_LEARN_26634_TO_26634(StockInstance):
    def dependency(self) -> Optional[Image]:
        return ImageDefault(self.pr, self._config)
