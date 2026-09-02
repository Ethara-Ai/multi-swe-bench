import json
import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


# ---------------------------------------------------------------------------
# typedoc spans three toolchain eras across the PRs in this dataset. The Node
# version is taken from the repo's OWN CI at each base commit, not guessed:
#
#   #936  2019-10-12  .travis.yml  node_js: 8    engines >=6.0.0   mocha dist/
#   #1169 2020-01-13  .travis.yml  node_js: 8    engines >=6.0.0   mocha dist/
#   #1412 2020-11-30  .travis.yml  node_js: 10   engines >=10.0.0  mocha dist/
#   #2078 2022-10-10  gh-actions   node: 16      engines >=14.14   ts-node
#   #2129 2023-03-05  gh-actions   node: 16      engines >=14.14   ts-node
#
# A single node:20 (the previous value) cannot install the 2019/2020
# package-lock.json trees, so the older PRs need their era's runtime.
#
# ONE SHARED BASE IMAGE serves all five PRs. Rather than three separate base
# images (one per era), the single base carries all three runtimes side by
# side: node 16 is the image's own, and 8 and 10 are unpacked into /opt. Each
# PR's scripts put its era's bin directory first on PATH, so every PR still
# runs on exactly the runtime its own CI used - the era mapping is unchanged,
# only where it is applied moved from image-selection time to run time.
_ERA_NODE_MAJOR = (
    (1200, "8"),    # PRs <= 1200  -> era A
    (1761, "10"),   # PRs <= 1761  -> era B
)
_DEFAULT_NODE_MAJOR = "16"

# Last release of each EOL major, pinned exactly. Both ship linux-x64 AND
# linux-arm64 tarballs, so the shared base stays multi-arch buildable.
_EXTRA_NODE_VERSIONS = ("8.17.0", "10.24.1")

_SHARED_BASE_RUNTIME = "node:16"


def _node_major(number: int) -> str:
    for upper, major in _ERA_NODE_MAJOR:
        if number <= upper:
            return major
    return _DEFAULT_NODE_MAJOR


def _node_path_line(number: int) -> str:
    """PATH export selecting this PR's era runtime inside the shared base.

    Node 16 is the base image's own interpreter and is already on PATH, so it
    needs no line; 8 and 10 live under /opt and must be prepended.
    """
    major = _node_major(number)
    if major == _DEFAULT_NODE_MAJOR:
        return ""
    return f'export PATH="/opt/node-{major}/bin:$PATH"\n'


def _pr_dockerfile(image: Image) -> str:
    """Dockerfile for a PR image on top of the SHARED base.

    Because ImageDefault.dependency() returns an Image rather than a string,
    DockerfileEnhancer.enhance() returns this text untouched - it injects
    nothing. Everything the enhancer would normally add to a base image and
    that still matters per-PR is therefore written out here explicitly: the
    syntax directive, the clone, the checkout of THIS PR's base commit, and
    the harness's own hardening block (reused verbatim from
    Image._HARDENING_BLOCK, not reimplemented).

    The proxy/CA ENV trust block does NOT need repeating: ENV is inherited
    from the shared base, so SSL_CERT_FILE and friends are already in force
    for the `git clone` and `npm ci` performed here.
    """
    pr = image.pr
    base = image.dependency()
    copy_commands = "".join(f"COPY {f.name} /home/\n" for f in image.files())

    return f"""# syntax=docker/dockerfile:1.6

FROM {base.image_name()}:{base.image_tag()}

{image.global_env}

ARG REPO_URL="https://github.com/{pr.org}/{pr.repo}.git"
ARG BASE_COMMIT="{pr.base.sha}"

{copy_commands}
RUN git clone "${{REPO_URL}}" /home/{pr.repo}

WORKDIR /home/{pr.repo}

RUN git reset --hard
RUN git checkout ${{BASE_COMMIT}}

{Image._HARDENING_BLOCK}

RUN bash /home/prepare.sh

{image.clear_env}

CMD ["/bin/bash"]
"""


class ImageBase(Image):
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
        # A STRING, so DockerfileEnhancer.enhance() runs and injects the shared
        # infrastructure (syntax directive, TARGETARCH/proxy/CA ARGs, the ENV
        # trust block, OCI labels, the CA symlink farm). Because this file
        # contains no `COPY <repo> /home/<repo>`, no `git clone`, `git fetch`
        # or `git remote add`, neither _standardize_repo_fetch nor
        # _inject_final_sanitize matches - so the enhancer adds the infra and
        # nothing else. That is exactly what makes ONE shared base possible:
        # the injected `git checkout ${BASE_COMMIT}` + ref-deleting hardening
        # would otherwise pin the single image to one PR's commit forever.
        # The clone, checkout and hardening move into each PR image instead,
        # where they are still applied per-PR and in full.
        return _SHARED_BASE_RUNTIME

    def image_tag(self) -> str:
        # SHARED across all five PRs. image_full_name() is what the harness
        # dedupes on, so this builds exactly one base image. Safe only because
        # this image holds no repository checkout at all - see dependency().
        return "base"

    def workdir(self) -> str:
        return "base"

    def files(self) -> list[File]:
        return []

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        # uname -m, not ${TARGETARCH}: the predefined platform ARGs are only
        # populated by BuildKit, and this config is built both ways (plain SDK
        # build for single-arch, buildx for the two-platform build). uname is
        # correct under both, and under QEMU emulation reports the TARGET arch.
        versions = " ".join(_EXTRA_NODE_VERSIONS)
        extra_runtimes = f"""RUN set -eux; \\
    arch="$(uname -m)"; \\
    case "$arch" in \\
        x86_64) narch=x64 ;; \\
        aarch64) narch=arm64 ;; \\
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \\
    esac; \\
    for v in {versions}; do \\
        major="${{v%%.*}}"; \\
        curl -fsSL --retry 5 --retry-delay 3 \\
            "https://nodejs.org/dist/v${{v}}/node-v${{v}}-linux-${{narch}}.tar.xz" \\
            -o /tmp/node.tar.xz; \\
        mkdir -p "/opt/node-${{major}}"; \\
        tar -xJf /tmp/node.tar.xz -C "/opt/node-${{major}}" --strip-components=1; \\
        rm -f /tmp/node.tar.xz; \\
        "/opt/node-${{major}}/bin/node" --version; \\
        "/opt/node-${{major}}/bin/npm" --version; \\
    done; \\
    node --version; \\
    npm --version"""

        return f"""FROM {image_name}

{self.global_env}

WORKDIR /home/

{extra_runtimes}

{self.clear_env}

CMD ["/bin/bash"]
"""


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

    def dependency(self) -> Image | None:
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# Select this PR's era runtime out of the shared base image.
{node_path}
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
npm ci --ignore-scripts || true

# HARD GATE - the install above is tolerant (|| true) because native
# rebuilds are flaky, so something must prove the tree is actually usable.
# Without this a failed install reaches the graded stages and surfaces as
# an unexplained empty report instead of a build failure.
if [ ! -d node_modules/.bin ]; then
    echo "FATAL: npm ci produced no node_modules" >&2
    exit 1
fi
node --version
echo "DEPS_OK"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
# -e is lifted only around the test call: at the test stage the suite is
# SUPPOSED to fail, and dying before the output is flushed would report zero
# tests and satisfy report.py's "fix something" check vacuously.
set +e
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
set +e
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
set +e
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
        ]

    def dockerfile(self) -> str:
        return _pr_dockerfile(self)


class ImageDefault1761(Image):
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
        return ImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            File(
                ".",
                "fix.patch",
                f"{self.pr.fix_patch}",
            ),
            File(
                ".",
                "test.patch",
                f"{self.pr.test_patch}",
            ),
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

""".format(),
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

# Select this PR's era runtime out of the shared base image.
{node_path}
cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh
git checkout {pr.base.sha}
bash /home/check_git_changes.sh
npm ci --ignore-scripts || true

# HARD GATE - the install above is tolerant (|| true) because native
# rebuilds are flaky, so something must prove the tree is actually usable.
# Without this a failed install reaches the graded stages and surfaces as
# an unexplained empty report instead of a build failure.
if [ ! -d node_modules/.bin ]; then
    echo "FATAL: npm ci produced no node_modules" >&2
    exit 1
fi
node --version
echo "DEPS_OK"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
# This era runs mocha against COMPILED dist/. `npm run build` must not abort
# the script under `set -e`: at the test stage the patched sources may not
# compile, and dying here would leave the stage with ZERO results - which
# satisfies report.py's "fix something" check vacuously. Capture both codes
# and always reach npm test.
set +e
npm run build
BUILD_RC=$?
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "BUILD_EXIT_CODE=$BUILD_RC"
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch
set +e
npm run build
BUILD_RC=$?
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "BUILD_EXIT_CODE=$BUILD_RC"
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

export CI=true
{node_path}
cd /home/{pr.repo}
git apply --whitespace=nowarn /home/test.patch /home/fix.patch
set +e
npm run build
BUILD_RC=$?
npm test -- --reporter json --timeout 120000
RC=$?
set -e
echo "BUILD_EXIT_CODE=$BUILD_RC"
echo "TEST_EXIT_CODE=$RC"

""".format(pr=self.pr, node_path=_node_path_line(self.pr.number)),
            ),
        ]

    def dockerfile(self) -> str:
        return _pr_dockerfile(self)


@Instance.register("TypeStrong", "typedoc")
class Typedoc(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        if self.pr.number <= 1761:
            return ImageDefault1761(self.pr, self._config)

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

    def parse_log(self, test_log: str) -> TestResult:
        # HANDOFF rule (b): parse machine-readable output, never console text.
        # All three stages run `npm test -- --reporter json`, so mocha emits a
        # single JSON document. It is NOT alone on stdout: npm prints a banner
        # before it, the suite logs warnings during the run, and (eras A/B) nyc
        # appends a coverage summary after it.
        #
        # Anchor on a column-0 "{" and hand the REST of the log to raw_decode,
        # which consumes exactly one JSON value and reports where it stopped.
        # Do NOT try to find the closing brace by matching a line equal to "}":
        # mocha does not terminate the document with a newline, so the script's
        # own `echo "TEST_EXIT_CODE=$RC"` lands on the same line ("}TEST_EXIT_
        # CODE=38") and no such line exists. raw_decode does not care what
        # follows the value, so it is immune to that and to any future trailer.
        decoder = json.JSONDecoder()
        report = None
        # Leading "\n" so a document starting at byte 0 is still found by the
        # "\n{" anchor below.
        haystack = "\n" + test_log
        offset = 0
        while True:
            start = haystack.find("\n{", offset)
            if start == -1:
                break
            start += 1
            try:
                candidate, _ = decoder.raw_decode(haystack, start)
            except ValueError:
                offset = start
                continue
            if isinstance(candidate, dict) and "stats" in candidate:
                report = candidate
                break
            offset = start

        if report is None:
            # No JSON document: the stage produced no usable result. Return an
            # empty TestResult rather than guessing from console text - an
            # honest zero surfaces as an invalid instance instead of silently
            # inventing passes.
            return TestResult(
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                passed_tests=set(),
                failed_tests=set(),
                skipped_tests=set(),
            )

        def _ids(bucket: str) -> set:
            # "fullTitle" is the suite-qualified name mocha builds itself, so
            # duplicate leaf names across suites (e.g. the several "matches
            # specs") stay distinct without reconstructing indentation. The
            # file is folded in too: the same fullTitle can legitimately come
            # from two spec files, and the id must be stable across stages.
            out = set()
            seen: dict[str, int] = {}
            for case in report.get(bucket) or []:
                if not isinstance(case, dict):
                    continue
                title = (case.get("fullTitle") or case.get("title") or "").strip()
                if not title:
                    continue
                title = re.sub(r"\s+", " ", title)
                path = (case.get("file") or "").strip()
                if path:
                    path = path.replace("\\", "/")
                    for root in ("/home/typedoc/", "/home/typedoc"):
                        if path.startswith(root):
                            path = path[len(root) :]
                            break
                    ident = f"{path}::{title}"
                else:
                    ident = title
                # typedoc really does define the same test title twice in one
                # suite (e.g. Events "listenTo and stopListening with event
                # maps"). A plain set would silently merge them and understate
                # the count, so suffix repeats by their order of appearance.
                # mocha emits cases in deterministic file order, so the suffix
                # is stable across the three stages.
                n = seen.get(ident, 0)
                seen[ident] = n + 1
                out.add(ident if n == 0 else f"{ident}#{n + 1}")
            return out

        passed_tests = _ids("passes")
        failed_tests = _ids("failures")
        skipped_tests = _ids("pending")

        # A case can appear in both "passes" and "failures" when mocha retries.
        # Failure wins so a flaky-but-failing test is never credited as passed.
        passed_tests -= failed_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )
