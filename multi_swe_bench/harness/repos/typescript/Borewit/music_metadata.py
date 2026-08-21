"""Borewit/music-metadata harness config.

Toolchain: Node.js 20, Yarn Berry 4 (vendored at .yarn/releases/yarn-berry.cjs,
nodeLinker: node-modules), Mocha 10 + Chai 5 running TypeScript directly through
the ts-node/esm loader configured in .mocharc.json.

Image layout — the `mvdan/sh.py` shape
--------------------------------------
  base-pr-<N>   clone, lift the patches' binary blobs, check out this PR's base
                commit, then the COMPLETE history scrub: `Image._HARDENING_BLOCK`
                verbatim, gc and repack and all four integrity asserts. Nothing
                is left over for the PR layer.

  pr-<N>        deliberately thin: stage the patches and run scripts, install
                dependencies, CMD. NO scrub block at all.

The scrub lives in the base and only in the base. It opens with
`git checkout --detach "${BASE_COMMIT}"`, so it can only run where BASE_COMMIT is
a real value — and it is real here because `dependency()` returns a str, which is
what makes build_dataset.py:625-629 pass REPO_URL and BASE_COMMIT as build args.
An Image-dependency layer receives no build args at all, so the same block in
`pr-<N>` would be scrubbing against a value it was handed by hand.

That is also why the tag is `base-pr-<N>` rather than a shared era tag. The prune
needs a pinned HEAD; pinning a SHARED base would fix it to whichever PR built it
first, and every other PR in the range would then die on `fatal: unable to read
tree`. One PR, one base, one prune. The dataset is a single PR, so this costs
nothing: two images either way.

Why the binary lift is in the base too
--------------------------------------
The dataset's patches were captured with plain `git diff`, without --binary, so a
new binary file shows up only as

    index 000000000..adab05dad
    Binary files /dev/null and b/test/samples/amr/ff-16b-1c-8000hz.amr differ

with no payload at all. `git apply` rejects the entire patch over those sections,
and the bytes cannot be reconstructed from the patch. They can only be read out of
the clone by their blob sha — and only before the history is stripped, because the
sample files were introduced by the PR's MERGE commit, a descendant of the base
commit, which `gc --prune=now` deletes. Confirmed inside the built image:
`git cat-file -e 4c63681…` fails.

So the lift has to sit on the same side of the scrub as the objects it reads.
Now that the scrub is in the base, the lift moves into the base with it, running
after the clone and before the prune. The blobs land in /home/binfiles/, OUTSIDE
the git repo where the scrub cannot reach them, and survive into `pr-<N>`.

Splitting then happens on BOTH sides, deliberately. Only the `git cat-file` blob
lift needs the pre-scrub objects; split_binary_patches.py is pure text processing
and touches no git object at all. So the base splits merely to learn which blob
shas to lift, and `pr-<N>` re-stages the PRISTINE patches and re-runs the split
itself, overwriting the base's stripped copies and regenerating the manifests
deterministically from the same input.

That split is what lets `COPY fix.patch` / `COPY test.patch` stay in the PR layer,
where the reference shape and the P-series QC expect to find them, without giving
up the base-owned scrub. The patches and their .binmanifest companions then belong
to the image that actually applies them, instead of arriving by inheritance from a
layer that stripped them for a different reason.

Test reporting
--------------
Mocha's `tap` reporter is used instead of the default `spec` reporter. spec prints
only the leaf title of a passing test, but spreads a failing test's title over
several indented lines of the numbered failure list, so pass names and fail names
never match and f2p comes out empty. TAP prints `test.fullTitle()` on a single
line for pass, fail and pending alike.

test/test-http.ts is excluded. It streams a sample over HTTP from
builds.tokyo.s3.amazonaws.com (and carries `this.retries(3)` for exactly that
reason), which makes it a coin flip inside the container and unrelated to any
patch under evaluation.
"""

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, DockerfileEnhancer, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest

# One place for the test command so run.sh / test-run.sh / fix-run.sh cannot
# drift apart. `yarn run test` is `mocha`; the binary is invoked directly so the
# reporter flag does not have to survive a yarn argument hand-off.
#
# --timeout raises mocha's 2000ms default. Measured on the first build: every one
# of the 433 baseline tests and 11 of the 12 AMR cases finish well inside 2000ms,
# but `parse: ff-16b-1c-8000hz.amr` under the FIRST parser does not. That sample
# is 299KB / ~187s of audio, so AmrParser walks ~9,400 frames one
# `tokenizer.ignore()` at a time, and the first parser variant also pays the
# ts-node cold-start cost. It passed under the other three parser variants in the
# same run -- i.e. it sits right on the line, which is the profile of a test that
# rotates between runs rather than one that is genuinely broken. A test that flips
# at random would silently move in and out of the f2p set.
#
# Only that one case is affected: no other test in any stage came near the limit,
# and the AMR failures in the test-patch stage are instant UnsupportedFileTypeError
# throws, so a longer timeout cannot mask them.
#
# A suite's own `this.timeout()` still wins over this flag (test-http.ts asks for
# 15s), which is deliberate; test-file-amr.ts sets none, so it inherits this.
TEST_CMD = (
    "./node_modules/.bin/mocha --reporter tap --timeout 30000"
    " --ignore 'test/test-http.ts'"
)

# Shared by the base (which strips the patches at build time) and the PR layer
# (which puts the blobs back at test time), so the manifest format is defined
# once. See the module docstring for why this exists at all.
SPLIT_BINARY_PATCHES_PY = '''#!/usr/bin/env python3
"""Split binary file diffs out of a patch, in place.

The dataset's patches were produced without `git diff --binary`, so a binary
file carries no payload -- only an `index <old>..<new>` line and a
"Binary files ... differ" marker. `git apply` refuses the whole patch over
those sections and the bytes are unrecoverable from the patch itself.

For every patch named on the command line this rewrites <patch> with the
binary sections removed, and writes <patch>.binmanifest with one TAB
separated record per binary file:

    A<TAB><blob-sha><TAB><path>    added or modified -- recover this blob
    D<TAB>-<TAB><path>             deleted -- remove this path

The blob sha is the patch's own post-image index, so the content can be read
straight out of the clone with `git cat-file blob` while the history is still
intact.
"""
import re
import sys

DIFF_SPLIT = re.compile(r"(?=^diff --git )", re.MULTILINE)
HEADER = re.compile(r"^diff --git a/(.*) b/(.*)$")
INDEX = re.compile(r"^index ([0-9a-f]+)\\.\\.([0-9a-f]+)", re.MULTILINE)
BINARY_MARKER = re.compile(r"^Binary files .* differ\\s*$", re.MULTILINE)
ZERO = re.compile(r"^0+$")


def split(path):
    with open(path, "r", errors="replace", newline="") as fh:
        content = fh.read()

    text_sections = []
    records = []

    for section in DIFF_SPLIT.split(content):
        if not section.strip():
            continue
        if "GIT binary patch" not in section and not BINARY_MARKER.search(section):
            text_sections.append(section)
            continue

        first_line = section.splitlines()[0]
        head = HEADER.match(first_line)
        if not head:
            # Cannot tell which path this belongs to; dropping it is still the
            # right call, because git apply would reject the whole patch.
            sys.stderr.write("split_binary_patches: unparsable header: %s\\n" % first_line)
            continue

        target = head.group(2).strip()
        idx = INDEX.search(section)
        new_sha = idx.group(2) if idx else ""
        if not new_sha or ZERO.match(new_sha):
            records.append(("D", "-", target))
        else:
            records.append(("A", new_sha, target))

    with open(path, "w", newline="\\n") as fh:
        fh.write("".join(text_sections))

    with open(path + ".binmanifest", "w", newline="\\n") as fh:
        for rec in records:
            fh.write("\\t".join(rec) + "\\n")

    sys.stderr.write(
        "split_binary_patches: %s -> %d binary record(s)\\n" % (path, len(records))
    )


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        split(arg)
'''


class MusicMetadataImageBase(Image):
    """Per-PR base for Borewit/music-metadata.

    Pinned to this PR's BASE_COMMIT and carrying the COMPLETE history scrub --
    gc, repack and all four integrity asserts -- which is why `pr-<N>` has no
    scrub block at all. See the module docstring for why the tag is per-PR and
    why the binary lift has to happen here rather than in the PR layer.
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
        return "node:20-bookworm"

    def image_tag(self) -> str:
        return f"base-pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"base-pr-{self.pr.number}"

    def files(self) -> list[File]:
        # The patches are staged HERE, not in pr-<N>, because extract-binaries.sh
        # rewrites them in place before the scrub. pr-<N> inherits the stripped
        # copies plus their .binmanifest companions.
        return [
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "split_binary_patches.py", SPLIT_BINARY_PATCHES_PY),
            File(
                ".",
                "extract-binaries.sh",
                """#!/bin/bash
# Build time only, and deliberately positioned AFTER the clone but BEFORE the
# hardening block. `gc --prune=now` deletes everything unreachable from
# BASE_COMMIT, and these blobs live in the PR's merge commit -- a DESCENDANT of
# it -- so after the scrub they are gone. This is the last moment they exist.
#
# The payload is written to /home/binfiles, OUTSIDE the repo, so the scrub that
# runs next cannot touch it.
set -euo pipefail

cd /home/{repo}

python3 /home/split_binary_patches.py /home/test.patch /home/fix.patch

mkdir -p /home/binfiles

for manifest in /home/test.patch.binmanifest /home/fix.patch.binmanifest; do
    [ -s "$manifest" ] || continue
    while IFS=$'\\t' read -r mode sha path; do
        [ -n "${{path:-}}" ] || continue
        [ "$mode" = "A" ] || continue
        # Resolve first and fail loudly. A redirection creates the target before
        # git runs, so an unresolvable sha would otherwise leave a zero byte
        # sample behind and surface much later as an unexplained test failure in
        # BOTH the test and the fix stage -- i.e. an empty f2p set.
        full_sha="$(git rev-parse --verify --quiet "${{sha}}^{{blob}}" || true)"
        if [ -z "$full_sha" ]; then
            echo "extract-binaries: FATAL cannot resolve blob $sha for $path" >&2
            exit 1
        fi
        mkdir -p "/home/binfiles/$(dirname "$path")"
        git cat-file blob "$full_sha" > "/home/binfiles/$path"
        size="$(stat -c%s "/home/binfiles/$path")"
        [ "$size" -gt 0 ] || echo "extract-binaries: WARNING $path is empty" >&2
        echo "extract-binaries: $sha -> $path ($size bytes)"
    done < "$manifest"
done
""".format(repo=self.pr.repo),
            ),
        ]

    def dockerfile(self) -> str:
        image_name = self.dependency()
        if isinstance(image_name, Image):
            image_name = image_name.image_full_name()

        if self.config.need_clone:
            code = f'RUN git clone "${{REPO_URL}}" /home/{self.pr.repo}'
        else:
            code = f"COPY {self.pr.repo} /home/{self.pr.repo}"

        label = (
            f'LABEL org.opencontainers.image.title="{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.description="{self.pr.org}/{self.pr.repo} Docker image" \\\n'
            f'      org.opencontainers.image.source="https://github.com/{self.pr.org}/{self.pr.repo}" \\\n'
            f'      org.opencontainers.image.authors="https://www.ethara.ai/"'
        )

        file_names = " ".join(file.name for file in self.files())

        # The COMPLETE scrub -- gc, repack and all four integrity asserts -- lives
        # here and only here. `Image._HARDENING_BLOCK` is used verbatim rather
        # than a hand-rolled variant so the asserts can never quietly diverge
        # from the harness's own definition; it already carries the submodule
        # pass as its second RUN.
        base_hardening = Image._HARDENING_BLOCK.rstrip("\n")

        # Proxy ARGs, the TLS/locale ENV block and the CA-cert symlink farm are
        # taken straight off DockerfileEnhancer rather than retyped, so they stay
        # byte-identical to what the enhancer injects elsewhere and cannot drift.
        #
        # They have to be written here by hand because enhance() bails out on the
        # first line of this file:
        #
        #     if cls.SYNTAX_DIRECTIVE in raw: return raw     (image.py:316-317)
        #
        # and the directive has to stay. Dropping it to re-enable the enhancer
        # would let _standardize_repo_fetch rewrite the clone and append a SECOND
        # copy of the hardening block.
        sections = [
            DockerfileEnhancer.SYNTAX_DIRECTIVE,
            f"FROM {image_name}",
            (
                f"{DockerfileEnhancer._TARGETARCH_ARG}\n"
                f'ARG REPO_URL="https://github.com/{self.pr.org}/{self.pr.repo}.git"\n'
                "# Supplied by the harness as a build arg. Declared BEFORE the\n"
                "# clone so a new sha busts the layer cache, and consumed by both\n"
                "# the checkout and the scrub below.\n"
                "ARG BASE_COMMIT\n"
                "\n"
                f"{DockerfileEnhancer._PROXY_ARGS}"
            ),
            DockerfileEnhancer._ENV_BLOCK,
            label,
            # Corepack asks "Do you want to continue?" before it downloads a
            # package manager. There is no TTY during a build, so without this
            # the very first `yarn` call in prepare.sh hangs or aborts.
            "ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \\\n"
            "    YARN_ENABLE_IMMUTABLE_INSTALLS=false \\\n"
            "    YARN_IGNORE_NODE=1",
            DockerfileEnhancer._CERT_SYMLINKS,
            "WORKDIR /home/",
            # python3 is for split_binary_patches.py, curl for the yarn/corepack
            # download path.
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            "        git ca-certificates curl python3 \\\n"
            "    && rm -rf /var/lib/apt/lists/*",
            "RUN corepack enable",
            code,
            f"WORKDIR /home/{self.pr.repo}",
            f"COPY {file_names} /home/",
            # Must precede the checkout+scrub below: it reads objects that the
            # prune is about to delete.
            "RUN bash /home/extract-binaries.sh",
            "RUN git reset --hard",
            "RUN git checkout ${BASE_COMMIT}",
            base_hardening,
            'CMD ["/bin/bash"]',
        ]
        return "\n\n".join(sections) + "\n"


class MusicMetadataImageDefault(Image):
    """Per-PR image: stage the run scripts and install dependencies.

    Carries no history scrub -- `base-pr-<N>` already ran the complete one -- and
    does not re-stage the patches, which the base already stripped in place.
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

    def dependency(self) -> Image:
        return MusicMetadataImageBase(self.pr, self._config)

    def image_tag(self) -> str:
        return f"pr-{self.pr.number}"

    def workdir(self) -> str:
        return f"pr-{self.pr.number}"

    def files(self) -> list[File]:
        return [
            # The PRISTINE patches, re-staged here rather than inherited. prepare.sh
            # re-runs the split on them so the stripped patches and their manifests
            # are produced by this image, not by the base.
            File(".", "fix.patch", f"{self.pr.fix_patch}"),
            File(".", "test.patch", f"{self.pr.test_patch}"),
            File(".", "split_binary_patches.py", SPLIT_BINARY_PATCHES_PY),
            File(
                ".",
                "restore-binaries.sh",
                """#!/bin/bash
# Test time. Puts the blobs saved at BASE image build time back into the
# worktree, after `git apply` has applied the text-only part of the same patch.
set -euo pipefail

cd /home/{repo}

for manifest in "$@"; do
    [ -s "$manifest" ] || continue
    while IFS=$'\\t' read -r mode sha path; do
        [ -n "${{path:-}}" ] || continue
        if [ "$mode" = "D" ]; then
            rm -f "$path"
        elif [ -f "/home/binfiles/$path" ]; then
            mkdir -p "$(dirname "$path")"
            cp "/home/binfiles/$path" "$path"
        else
            echo "restore-binaries: MISSING /home/binfiles/$path" >&2
            exit 1
        fi
    done < "$manifest"
done
""".format(repo=self.pr.repo),
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
""",
            ),
            File(
                ".",
                "prepare.sh",
                """#!/bin/bash
set -e

cd /home/{repo}

git reset --hard
bash /home/check_git_changes.sh
git checkout "${{BASE_COMMIT}}"
bash /home/check_git_changes.sh

# The patches COPY'd into this layer are the pristine originals, so strip their
# binary sections here. This is pure text processing -- no git object is touched --
# which is why it can run on this side of the base's history scrub. It overwrites
# the base's own stripped copies with an identical result.
python3 /home/split_binary_patches.py /home/test.patch /home/fix.patch

# Every payload the manifests name must already be sitting in /home/binfiles,
# lifted by the base before its scrub pruned the objects. Checking at build time
# turns a broken lift into an obvious build failure instead of a test-stage
# failure that reads like a bug in the repo.
for manifest in /home/test.patch.binmanifest /home/fix.patch.binmanifest; do
    test -f "$manifest"
    while IFS=$'\\t' read -r mode sha path; do
        [ -n "${{path:-}}" ] || continue
        [ "$mode" = "A" ] || continue
        test -s "/home/binfiles/$path"
    done < "$manifest"
done
! grep -q '^Binary files ' /home/test.patch
! grep -q '^Binary files ' /home/fix.patch

# .yarnrc.yml pins yarnPath to the vendored .yarn/releases/yarn-berry.cjs, so
# this only needs corepack's shim to exist in place of the classic yarn 1.x
# that the node image ships.
corepack enable

# --immutable makes yarn FAIL rather than silently rewrite yarn.lock. The repo
# vendors the very yarn that produced the lockfile (4.3.1), so there is no version
# skew to drift it. The CLI flag deliberately overrides the base image's
# YARN_ENABLE_IMMUTABLE_INSTALLS=false, which stays in place as the default so the
# dependency-changing reinstall in test-run.sh / fix-run.sh may still update the
# lockfile -- that one has to be allowed to mutate.
yarn install --immutable

# yarn must not have altered a tracked file. The checks above run BEFORE the
# install, so without this one a rewritten lockfile would ship unnoticed.
bash /home/check_git_changes.sh
""".format(repo=self.pr.repo),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

# Mirrors CI, which builds before testing: with lib/**/*.js present mocha's
# ts-node/esm loader resolves the tests' `../lib/index.js` imports to the
# compiled output. Failure is tolerated because ts-node can also compile
# lib/**/*.ts on the fly.
yarn run compile-src || true

{test_cmd} 2>&1 || true
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch
bash /home/restore-binaries.sh /home/test.patch.binmanifest

if grep -qE '^diff --git a/(package\\.json|yarn\\.lock)' /home/test.patch; then
    yarn install || true
fi

yarn run compile-src || true

{test_cmd} 2>&1 || true
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
set -eo pipefail

cd /home/{repo}

git apply --whitespace=nowarn /home/test.patch /home/fix.patch
bash /home/restore-binaries.sh /home/test.patch.binmanifest /home/fix.patch.binmanifest

if grep -qhE '^diff --git a/(package\\.json|yarn\\.lock)' /home/test.patch /home/fix.patch; then
    yarn install || true
fi

yarn run compile-src || true

{test_cmd} 2>&1 || true
""".format(repo=self.pr.repo, test_cmd=TEST_CMD),
            ),
        ]

    def dockerfile(self) -> str:
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        # One COPY per file, matching the reference PR Dockerfile line for line.
        # A single multi-file COPY would be functionally identical, but the
        # per-file form leaves no room to argue about whether each expected
        # artifact is staged.
        copy_command = "\n".join(f"COPY {file.name} /home/" for file in self.files())

        # Deliberately thin. No clone, no apt, no CA/proxy setup and NO history
        # scrub -- {tag} is pinned to this PR's base commit and has already run
        # the full scrub (gc, repack, all four asserts), so there is nothing left
        # to prune here. Repeating it would only re-run an expensive no-op.
        return f"""FROM {name}:{tag}

{self.global_env}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

WORKDIR /home/{self.pr.repo}

{copy_command}

RUN bash /home/prepare.sh

{self.clear_env}

CMD ["/bin/bash"]
"""


@Instance.register("Borewit", "music-metadata")
class BorewitMusicMetadata(Instance):
    """Harness instance for Borewit/music-metadata — Mocha TAP output."""

    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
        return MusicMetadataImageDefault(self.pr, self._config)

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
        """Parse Mocha's TAP reporter output.

            1..12345
            ok 1 Adaptive Multi-Rate (AMR) audio file parser.description parse: sample.amr
            not ok 2 ID3v2.4 should read TXXX frame
              ---
              ...
              ...
            ok 3 some pending test # SKIP -
            # tests 12345

        The name is `test.fullTitle()`, identical on the pass, fail and pending
        lines, which is what keeps the f2p/p2p set comparison meaningful.

        Titles repeat where a suite loops over the four entries of
        test/metadata-parsers.ts inside a `describe('parser.description')` whose
        title is a literal string rather than a template -- test-file-amr.ts does
        this, so its four variants share one full title and collapse into a single
        set member. Stable across all three stages, so it costs granularity, not
        correctness. Most other suites use `describe(parser.description)` properly
        and keep their per-parser suffix.
        """
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        clean_log = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", test_log)

        re_ok = re.compile(r"^ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_not_ok = re.compile(r"^not ok\s+\d+\s*(?:-\s+)?(.*)$")
        re_directive = re.compile(r"\s+#\s*(SKIP|TODO)\b.*$", re.IGNORECASE)

        for line in clean_log.splitlines():
            line = line.rstrip()

            m = re_not_ok.match(line)
            if m:
                name = re_directive.sub("", m.group(1)).strip()
                if name:
                    failed_tests.add(name)
                continue

            m = re_ok.match(line)
            if m:
                raw_name = m.group(1)
                is_skip = bool(re_directive.search(raw_name))
                name = re_directive.sub("", raw_name).strip()
                if not name:
                    continue
                if is_skip:
                    skipped_tests.add(name)
                else:
                    passed_tests.add(name)

        # A title that failed anywhere wins over the same title passing
        # elsewhere, so the collapsed duplicates never look green by accident.
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
