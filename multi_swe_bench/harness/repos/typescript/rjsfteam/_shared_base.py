"""Shared base image for BOTH rjsf-team/react-jsonschema-form eras (#3916 - #4925).

This module defines no Instance and registers nothing.  It exists so the two
era configs in this directory -

    react_jsonschema_form_lerna_3916_to_4669.py   (12 PRs, lerna 6 toolchain)
    react_jsonschema_form_nx_4352_to_4925.py      ( 8 PRs, nx 22 toolchain)

- can share ONE base image instead of building one apiece.

WHY A SHARED MODULE RATHER THAN A COPY IN EACH ERA FILE
-------------------------------------------------------
Image de-duplication keys on ``image_full_name()`` = ``image_name()`` + ":" +
``image_tag()``, and ``image_name()`` is derived from org/repo, so it is the
same for every config of this repo.  Two eras returning the same
``image_tag()`` therefore resolve to ONE image and only the era that builds
first actually produces it - the other is silently skipped and inherits
whatever the winner built.

That makes a duplicated base definition genuinely dangerous: if someone later
edits the base in one era file and not the other, the resulting image depends
on build order, which is non-deterministic.  Defining it once here removes the
possibility.  (This is a deliberate departure from the "each era file is fully
self-contained" convention: once the eras are required to share an image, the
sharing is a fact, and making it explicit is safer than leaving it implicit in
two copies that are only equal by luck.)

The eras still keep everything that genuinely differs - build command, test
command, prepare.sh assertions and parse_log - in their own files.  Only the
OS/runtime layer is shared.

NODE 22 FOR BOTH ERAS - MEASURED
--------------------------------
The eras were originally split onto node:18 and node:22 because the repo's own
pins differ::

    era     .nvmrc  .node-version  engines.node
    lerna   18      18.16.0        >=14
    nx      22      22.13.1        >=20

node:18 cannot serve the nx era (``engines.node: ">=20"`` rejects it), so the
only possible shared runtime is Node 22.  The lerna era declares ``>=14``, so
Node 22 satisfies it on paper, and every pinned dev-tool's own ``engines.node``
range is open-ended above 18::

    jest / ts-jest / babel-jest 29.6.4   ^14.15.0 || ^16.10.0 || >=18.0.0
    lerna 6.6.2                          ^14.17.0 || >=16.0.0
    rollup 3.29.0                        >=14.18.0
    esbuild 0.18.20                      >=12
    jsdom 20.0.3                         >=14

Declared support is not proof, so the lerna era was re-run end to end on
node:22-bookworm at its OLDEST base commit (PR #3916, ``fb7adf28ba``) - the
commit most likely to break - and compared against the node:18 baseline::

                            node:18   node:22
    packages reporting           11        11
    total tests                3967      3967
    per-test tick lines        3967      3967
    failed tests                  0         0
    per-package differences            NONE

Install, build and test all exited 0.  The two runs are indistinguishable, so
the lerna era loses nothing by moving to Node 22.

IMAGE TAG - WHY NOT JUST "base"
-------------------------------
``reactjsonschemaform.py`` in this directory is the generic config for an
earlier phase (PRs #1993 - #3763) and its base image is ALSO published under
this repo's ``image_name()`` with ``image_tag() == "base"`` on ``node:18``.
Taking "base" here would collide with it, and because de-duplication silently
keeps whichever image already exists, that would either starve these PRs of
Node 22 or overwrite an image the earlier phase's PRs still depend on.  The
tag below is therefore explicit about the PR span it serves.

DOCKERFILE / ENHANCER CONTRACT
------------------------------
The Dockerfile carries ``# syntax=docker/dockerfile:1.6`` on purpose.
``DockerfileEnhancer.enhance()`` returns the Dockerfile untouched when that
directive is already present, so the infrastructure block (proxy args, env,
cert symlinks, labels) is composed here from the enhancer's own constants
instead of being injected.  This is required, not cosmetic: the enhancer would
otherwise append ``Image._HARDENING_BLOCK`` to this base, and the base is now
shared by all 20 PRs across both eras.  Hardening prunes every object
unreachable from one ``BASE_COMMIT``, so the first PR to build would pin the
base and every other PR's checkout would die with ``fatal: reference is not a
tree``.  Hardening therefore runs in the per-PR layer, where ``BASE_COMMIT`` is
that PR's own sha.
"""

from typing import Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.pull_request import PullRequest

# Spans both eras: lerna #3916-#4669 and nx #4352-#4925.  Deliberately not
# "base" - see the module docstring.
SHARED_BASE_TAG = "base-react_jsonschema_form_3916_to_4925"

# Only runtime that can serve both eras: the nx era's engines.node is ">=20".
BASE_IMAGE = "node:22-bookworm"

# NODE_OPTIONS: the monorepo builds up to 15 packages and jest runs two package
# workers at a time; the default old-space is not enough for @rjsf/core.
# NX_SKIP_NX_CACHE: load-bearing for BOTH eras.  lerna 6 dispatches through nx
# and nx caches task output; a replayed task prints jest's raw output WITHOUT
# the "<package>: " stream prefix that parse_log keys on, so a cache hit yields
# a 0/0/0 TestResult.  Measured: the fix stage of PR #3916 replayed 11 of 11
# tasks from cache and parsed zero tests.
TOOLCHAIN_ENV = (
    "NODE_OPTIONS=--max-old-space-size=4096",
    "NX_SKIP_NX_CACHE=true",
    "NPM_CONFIG_FUND=false",
    "NPM_CONFIG_AUDIT=false",
    "NPM_CONFIG_UPDATE_NOTIFIER=false",
)


_BASE_DOCKERFILE = r"""# syntax=docker/dockerfile:1.6

FROM __BASE_IMAGE__

ARG TARGETARCH
ARG REPO_URL="https://github.com/__ORG__/__REPO__.git"
ARG BASE_COMMIT

__PROXY_ARGS__

__ENV_BLOCK__

LABEL org.opencontainers.image.title="__ORG__/__REPO__" \
      org.opencontainers.image.description="__ORG__/__REPO__ Docker image" \
      org.opencontainers.image.source="https://github.com/__ORG__/__REPO__" \
      org.opencontainers.image.authors="https://www.ethara.ai/"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    build-essential \
    git \
    gnupg \
    make \
    python3 \
    sudo \
    wget \
    && rm -rf /var/lib/apt/lists/*

__CERT_SYMLINKS__

__CLEAR_ENV__

WORKDIR /home/

RUN git clone "${REPO_URL}" /home/__REPO__

CMD ["/bin/bash"]
"""


class RjsfSharedImageBase(Image):
    """One base image for both eras: OS packages, Node 22, and a bare clone.

    No checkout and no hardening happen here.  Both are done in the per-PR
    layer so that each PR's own ``base.sha`` stays reachable in the shared
    image's git history.
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

    def dependency(self) -> Union[str, "Image"]:
        return BASE_IMAGE

    def image_tag(self) -> str:
        return SHARED_BASE_TAG

    def workdir(self) -> str:
        return SHARED_BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        base_image = self.dependency()
        if isinstance(base_image, Image):
            base_image = base_image.image_full_name()

        return (
            _BASE_DOCKERFILE.replace("__BASE_IMAGE__", base_image)
            .replace("__ORG__", self.pr.org)
            .replace("__REPO__", self.pr.repo)
            .replace("__PROXY_ARGS__", DockerfileEnhancer._PROXY_ARGS)
            .replace("__ENV_BLOCK__", self._merged_env_block())
            .replace("__CERT_SYMLINKS__", DockerfileEnhancer._CERT_SYMLINKS)
            .replace("__CLEAR_ENV__", self.clear_env)
        )

    def _merged_env_block(self) -> str:
        """Enhancer ENV block + any --global_env vars + toolchain vars, as one
        ENV instruction.  clear_env stays separate: its job is to blank the
        global vars again at the end of the file."""
        assignments = [
            line[len("ENV ") :]
            for line in self.global_env.splitlines()
            if line.startswith("ENV ")
        ]
        assignments.extend(TOOLCHAIN_ENV)
        return DockerfileEnhancer._ENV_BLOCK + "".join(
            " \\\n    " + assignment for assignment in assignments
        )
