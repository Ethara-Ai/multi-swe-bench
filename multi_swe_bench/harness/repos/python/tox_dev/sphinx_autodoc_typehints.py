"""Repo config for ``tox-dev/sphinx-autodoc-typehints`` (Python / pytest).

Registration
------------
Every entry in ``tox-dev__sphinx-autodoc-typehints_raw_dataset.jsonl`` carries
``number_interval == ""`` and ``tag == ""``, so ``Instance.create()``
(instance.py:40-51) builds the lookup key ``"tox-dev/sphinx-autodoc-typehints"``.
A single registration therefore serves the whole dataset, and the per-era
toolchain differences are resolved *inside* this file by branching on
``pr.number`` (see ``_ERAS``).

Era map
-------
The dataset spans PR #171 (base commit 2022-01-08) to PR #630 (base commit
2026-02-24). Over that window the project changed build backend, dependency
declaration style, minimum Python and minimum Sphinx several times, so a single
image cannot serve all of it. Each boundary below is a real commit in the
upstream history, not an estimate::

    ccc75d2  2022-09-14  "Move to hatchling from setuptools"          -> setup.cfg dropped,
                                                                        requires-python >=3.7
                         (direct push between PR #252 and PR #254, so the
                          PR-number boundary is 253)
    ff8ab27  2023-04-19  "Bump deps and tools (#348)"                 -> >=3.8, Sphinx>=6.1.3
    7684520  2024-04-17  "Support Sphinx 7.3 and drop 3.8 ... (#448)" -> >=3.9, Sphinx>=7.3.5
    f15864e  2024-09-07  "Drop 3.9 support"                           -> >=3.10, sphinx>=8.0.2
                         (direct push between PR #474 and PR #478, so the
                          PR-number boundary is 475)
    ca5fcc0  2025-02-19  "Support Sphinx 8.2.0 - drop 3.10 ... (#525)"-> >=3.11, sphinx>=8.2
    c9ee0e3  2026-01-02  "Fix compatibility with 9.1.0 (#595)"        -> >=3.12, sphinx>=9.1
    07804ac  2026-02-19  "Move from extras to dependency-groups"      -> PEP 735 groups
                         (#612)                                         replace
                                                                        optional-dependencies

``_ERAS`` is ordered and total: ``_era_for()`` always returns an era for any
non-negative PR number, so neither ``dependency()`` nor ``image_tag()`` can
return ``None`` and no PR number is left unrouted.

Why the Sphinx / pytest pins exist
----------------------------------
Both are load-bearing, not cosmetic:

* ``tests/conftest.py`` imports ``sphinx.testing.path`` up to and including the
  Sphinx 8 era; that module was removed in Sphinx 8.0. The project's own
  ``dependencies`` entry is an *unbounded* floor (``Sphinx>=4``), so an
  unconstrained install resolves to today's Sphinx and breaks historic commits.
  Each era is therefore pinned ``>= its own floor, < the next era's floor``.
* For PR #171's era the ``sphinxcontrib-*`` helper packages must also be held at
  their 1.x releases: the 2.x line declares ``Sphinx>=5`` and makes every
  ``@pytest.mark.sphinx`` test raise ``VersionRequirementError`` at fixture
  setup, which is exactly where PR #171's target test lives.
* ``tests/conftest.py`` used the legacy ``pytest_ignore_collect(path, ...)``
  signature until PR #514 switched it to ``collection_path``; pytest 9 removed
  the legacy parameter, hence the ``<9`` ceilings on the older eras.

Verified locally (docker, linux/amd64) before this config was written:

* PR #171  ``python:3.10-slim``  run 103 passed / test 56 failed + 50 passed /
  fix 106 passed  -> clean F2P transition.
* PR #459  ``python:3.12-slim``  306 passed, 0 failed.
* PR #630  ``python:3.14-slim``  464 passed, 1 skipped, 4 failed (the 4 are
  ``test_format_annotation`` role assertions that compare against the published
  CPython inventory and fail identically in all three stages).
"""

import re
from typing import NamedTuple

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# ``pip install -e ".[testing]"`` installs the project *and* its test extra in
# one shot. Used by every era before PR #612.
_INSTALL_EXTRAS = 'python -m pip install --no-cache-dir -e ".[testing]" || true'

# From PR #612 the test dependencies live in a PEP 735 ``[dependency-groups]``
# table, which ``--group`` reads (pip >= 25.1). ``--group`` installs only the
# group, so the project itself still needs a separate editable install.
_INSTALL_GROUP = (
    "python -m pip install --no-cache-dir --group test || true\n"
    'python -m pip install --no-cache-dir -e "." || true'
)


# Measured, do not "upgrade" blindly: era C = 467 pass/0 fail on 3.12 vs 464/4 on
# 3.14; era B = 306/0; era A = 100/3 (the 3 are golden-output tests absent from
# PR #171's test.patch, so they fail in all stages and are never credited).
_BASE_IMAGE = "python:3.12-slim"
_BASE_TAG = "base"


class _Era(NamedTuple):
    """``below`` is the exclusive PR-number bound; ``None`` = open-ended tail."""

    below: int | None
    pins: tuple[str, ...]
    install: str


_ERAS: tuple[_Era, ...] = (
    # --- setuptools + setup.cfg, python_requires >=3.7, Sphinx>=4 -------------
    # Covers PR #171. On 3.12: 100 passed, 3 failed (golden-output drift).
    _Era(
        below=253,
        pins=(
            "Sphinx>=4,<5",
            # 2.x of these require Sphinx>=5 and break every sphinx fixture.
            "sphinxcontrib-applehelp==1.0.2",
            "sphinxcontrib-devhelp==1.0.2",
            "sphinxcontrib-htmlhelp==2.0.0",
            "sphinxcontrib-jsmath==1.0.1",
            "sphinxcontrib-qthelp==1.0.3",
            "sphinxcontrib-serializinghtml==1.1.5",
            "alabaster==0.7.13",
            "pytest>=6,<8",
        ),
        install=_INSTALL_EXTRAS,
    ),
    # --- hatchling, requires-python >=3.7, Sphinx>=5.1.1 ----------------------
    _Era(
        below=348,
        pins=("Sphinx>=5.1.1,<6", "pytest>=7.1.3,<8"),
        install=_INSTALL_EXTRAS,
    ),
    # --- requires-python >=3.8, Sphinx>=6.1.3 --------------------------------
    _Era(
        below=448,
        pins=("Sphinx>=6.1.3,<7", "pytest>=7,<8"),
        install=_INSTALL_EXTRAS,
    ),
    # --- requires-python >=3.9, Sphinx>=7.3.5 --------------------------------
    # Covers PR #459. On 3.12: 306 passed, 0 failed.
    _Era(
        below=475,
        pins=("sphinx>=7.3.5,<8", "pytest>=8.1.1,<9"),
        install=_INSTALL_EXTRAS,
    ),
    # --- requires-python >=3.10, sphinx>=8.0.2 -------------------------------
    _Era(
        below=525,
        pins=("sphinx>=8.0.2,<8.2", "pytest>=8,<9"),
        install=_INSTALL_EXTRAS,
    ),
    # --- requires-python >=3.11, sphinx>=8.2 ---------------------------------
    _Era(
        below=595,
        pins=("sphinx>=8.2,<9", "pytest>=8,<9"),
        install=_INSTALL_EXTRAS,
    ),
    # --- requires-python >=3.12, sphinx>=9.1, still optional-dependencies -----
    _Era(
        below=612,
        pins=("sphinx>=9.1,<10", "pytest>=9,<10"),
        install=_INSTALL_EXTRAS,
    ),
    # --- PEP 735 dependency-groups (PR #612 onwards) -------------------------
    # Covers PRs #616, #629, #630. VERIFIED on python:3.14-slim (pip 26.2.1
    # supports ``--group``): 464 passed, 1 skipped, 4 failed.
    _Era(
        below=None,
        pins=("sphinx>=9.1,<10", "pytest>=9,<10"),
        install=_INSTALL_GROUP,
    ),
)


def _era_for(number: int) -> _Era:
    """Resolve the toolchain era for a PR number. Total by construction."""
    for era in _ERAS:
        if era.below is None or number < era.below:
            return era
    return _ERAS[-1]


# tests/conftest.py downloads the CPython ``objects.inv`` from docs.python.org on
# first use of the session-scoped ``inv`` fixture. The three stages run in three
# concurrent containers, so that is three simultaneous 3 MB fetches per instance
# and a live dependency on an external host; when it fails the whole
# ``test_format_annotation`` family errors out (observed: 80 errors on an
# otherwise green tree). Priming pytest's own cache at image-build time makes all
# three stages read one identical, already-downloaded inventory instead.
# ``.pytest_cache`` matches the repo's ``.*_cache`` gitignore rule in every era,
# so it never dirties the tree or interferes with ``git apply``.
_PRIME_OBJECTS_INV = (
    'python -c "import json,pathlib,sys;from sphobjinv import Inventory;'
    "v='%d.%d'%sys.version_info[:2];"
    "p=pathlib.Path('.pytest_cache/v')/('python'+v)/'objects.inv';"
    "p.parent.mkdir(parents=True,exist_ok=True);"
    "p.write_text(json.dumps(Inventory("
    "url='https://docs.python.org/'+v+'/objects.inv').json_dict()))\" || true"
)

# Identical in run.sh / test-run.sh / fix-run.sh so the three stages compare
# like for like.
#   -v                              one ``<nodeid> STATUS [ nn%]`` line per test,
#                                   which is what parse_log() consumes.
#   --continue-on-collection-errors a test file added by test.patch usually
#                                   imports a symbol that only fix.patch adds
#                                   (e.g. PR #629 -> ``_resolver.get_obj_location``).
#                                   Without this flag pytest aborts the whole
#                                   session on that ImportError and the stage
#                                   yields zero results.
#   --color=no                      belt and braces with the ANSI strip in parse_log.
_TEST_CMD = (
    "python -m pytest tests -v --no-header --color=no --continue-on-collection-errors"
)


class SphinxAutodocTypehintsImageBase(Image):
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
        return _BASE_IMAGE

    def image_tag(self) -> str:
        return _BASE_TAG

    def workdir(self) -> str:
        return _BASE_TAG

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        """Base image: toolchain + a plain full clone. Nothing else.

        The base image is shared by every PR in its era, so it deliberately does
        NOT check out ``BASE_COMMIT`` and does NOT run the git-hardening block --
        both of those are per-PR concerns and live in the PR image.

        Emitting ``SYNTAX_DIRECTIVE`` ourselves makes ``DockerfileEnhancer``
        return this text verbatim (image.py:317-318), which is what stops the
        enhancer's ``_standardize_repo_fetch`` from appending checkout+hardening
        here. The infrastructure block is generated by the enhancer's own
        ``_infrastructure_block`` so the ARG/ENV/LABEL/cert preamble stays
        byte-identical to the pipeline standard instead of being re-typed.
        """
        repo = self.pr.repo

        blocks = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {_BASE_IMAGE}",
            DockerfileEnhancer._infrastructure_block(self, _BASE_IMAGE).rstrip("\n"),
        ]

        if self.global_env:
            blocks.append(self.global_env)

        blocks.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "    ca-certificates \\\n"
            "    curl \\\n"
            "    git \\\n"
            "    graphviz \\\n"
            "    build-essential \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
        blocks.append(
            "RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel"
        )
        blocks.append("WORKDIR /home/")
        blocks.append(f'RUN git clone "${{REPO_URL}}" /home/{repo}')
        blocks.append('CMD ["/bin/bash"]')

        return "\n\n".join(blocks) + "\n"


class SphinxAutodocTypehintsImageDefault(Image):
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
        return SphinxAutodocTypehintsImageBase(self.pr, self.config)

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
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}
git reset --hard
git checkout {base_sha}

python -m pip install --no-cache-dir \\
    {pins} || true

{install}

{prime}
""".format(
                    repo=self.pr.repo,
                    base_sha=self.pr.base.sha,
                    pins=" \\\n    ".join(
                        f'"{spec}"' for spec in _era_for(self.pr.number).pins
                    ),
                    install=_era_for(self.pr.number).install,
                    prime=_PRIME_OBJECTS_INV,
                ),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=_TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail
export CI=true

cd /home/{repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
{test_cmd}
""".format(repo=self.pr.repo, test_cmd=_TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        """PR image: pin to this PR's base commit, then strip the git history.

        The hardening block lives here rather than in the base image or
        prepare.sh because it is per-PR: it detaches at *this* PR's
        ``base.sha`` and then deletes every other ref, so the agent cannot read
        the future of the branch. ``BASE_COMMIT`` is declared as an ARG default
        because build_dataset only passes build args to base images
        (build_dataset.py:623-630); taking it from ``self.pr.base.sha`` keeps
        the PR image self-contained and reproducible.

        ``Image._HARDENING_BLOCK`` is reused verbatim so this stays in lockstep
        with the pipeline's canonical hardening rather than drifting from a copy.
        """
        dep = self.dependency()
        repo = self.pr.repo

        blocks = [
            f"FROM {dep.image_name()}:{dep.image_tag()}",
            f'ARG BASE_COMMIT="{self.pr.base.sha}"',
        ]

        if self.global_env:
            blocks.append(self.global_env)

        blocks.append(
            "COPY fix.patch /home/\n"
            "COPY test.patch /home/\n"
            "COPY prepare.sh /home/\n"
            "COPY run.sh /home/\n"
            "COPY test-run.sh /home/\n"
            "COPY fix-run.sh /home/"
        )
        blocks.append("RUN bash /home/prepare.sh")
        blocks.append(f"WORKDIR /home/{repo}")
        blocks.append(Image._HARDENING_BLOCK.rstrip("\n"))

        if self.clear_env:
            blocks.append(self.clear_env)

        return "\n\n".join(blocks) + "\n"


@Instance.register("tox-dev", "sphinx-autodoc-typehints")
class SphinxAutodocTypehints(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Image:
        return SphinxAutodocTypehintsImageDefault(self.pr, self._config)

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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        # Strip ANSI first: colour codes would otherwise sit between the node id
        # and the status word and defeat the anchor below.
        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        # ``pytest -v`` emits exactly one line per test:
        #     tests/test_annotations.py::test_parse_annotation[str] PASSED  [  0%]
        #
        # Two properties of this project's node ids drive the pattern:
        #  * parametrised ids embed spaces, commas, backticks and brackets
        #    (e.g. ``test_format_annotation[Mapping-:py:class:`~typing.Mapping`\\[
        #    \\~T, :py:class:`int`]]``), so the id cannot be matched with ``\\S+``;
        #    it is captured lazily and delimited by the trailing progress column.
        #  * the ``[ nn%]`` progress counter is the only variable metadata on the
        #    line and is deliberately left outside the capture group, so a test
        #    yields a byte-identical name in all three stages even though its
        #    ordinal position shifts when test.patch adds files.
        #
        # The ``-r`` short-summary lines ("FAILED <id> - <msg>") are intentionally
        # NOT parsed: they truncate ids that contain spaces, which would inject a
        # second, malformed name for the same test. Verified against real logs
        # that the verbose lines alone account for 100% of collected tests.
        verbose_line = re.compile(
            r"^(.+?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+\[\s*\d+%\]\s*$"
        )

        for line in clean_log.split("\n"):
            match = verbose_line.match(line.rstrip())
            if not match:
                continue

            test_name = match.group(1).strip()
            status = match.group(2)

            if status in ("PASSED", "XPASS"):
                passed_tests.add(test_name)
            elif status in ("FAILED", "ERROR"):
                failed_tests.add(test_name)
            else:  # SKIPPED, XFAIL
                skipped_tests.add(test_name)

        # TestResult.__post_init__ rejects any overlap between the three sets.
        # Ordering matters: failures win over passes and skips, skips win over
        # passes, and the last subtraction cannot reintroduce a failed test.
        passed_tests -= failed_tests
        skipped_tests -= failed_tests
        passed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
