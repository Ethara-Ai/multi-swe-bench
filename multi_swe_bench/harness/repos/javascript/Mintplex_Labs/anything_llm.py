from __future__ import annotations

import re
from typing import Optional, Union

from multi_swe_bench.harness.image import Config, File, Image
from multi_swe_bench.harness.instance import Instance, TestResult
from multi_swe_bench.harness.pull_request import PullRequest


class ImageBase(Image):
    """Level 1: shared node:20 toolchain base (built once, reused by every PR).

    dependency() returns a *string* (node:20-bookworm), which normally makes the
    DockerfileEnhancer prepend its infra block. This Dockerfile therefore carries
    the ``# syntax=docker/dockerfile:1.6`` directive itself, which makes
    DockerfileEnhancer.enhance() return it verbatim -- so NO proxy build args, no
    CA-certificate symlink block, and no MITM certificate secret mount are
    injected into this image. The pieces of the infra block that are still wanted
    (TARGETARCH/REPO_URL/BASE_COMMIT args, the OCI labels, DEBIAN_FRONTEND/LANG/
    TZ) are declared inline below instead. The `ca-certificates` apt package is
    deliberately kept: that is the distro trust store that plain HTTPS (git clone,
    yarn) needs, not proxy/MITM interception config.

    Opting out costs nothing else here: the enhancer's other two passes
    (_standardize_repo_fetch, _inject_final_sanitize) only act on Dockerfiles
    that clone, and this one deliberately does not (see below).

    IMPORTANT: this image must NOT clone the repo. The tag is a constant
    ("base"), so a single image is shared by all 12 PRs in the dataset -- but a
    string-dependency image that clones is rewritten by the enhancer into
    clone + ``git checkout ${BASE_COMMIT}`` + Image._HARDENING_BLOCK, which
    force-pins it to whichever PR happened to build the base first and strips
    every other commit out of the history (all refs deleted, remote removed,
    reflog expired, ``git gc --prune=now``). Every PR whose base commit is not
    an ancestor of that pinned commit then fails ``git checkout`` with no remote
    left to refetch from. So the clone lives per-PR in ImageDefault (an Image
    dependency, left verbatim by the enhancer). This image only provides node +
    the apt build deps, shared across all PRs.
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

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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

        # No `git clone`/`COPY` of the repo here on purpose (see class docstring).
        # The `# syntax` directive below makes DockerfileEnhancer.enhance() return
        # this file verbatim, so no proxy args / cert symlinks / MITM mount are
        # injected. REPO_URL and BASE_COMMIT are declared (unused by this layer,
        # which does not clone) purely so the build args build_dataset passes for
        # string-dependency images do not trigger "unused build arg" warnings.
        org, repo = self.pr.org, self.pr.repo
        return f"""# syntax=docker/dockerfile:1.6
FROM {image_name}

ARG TARGETARCH
ARG REPO_URL="https://github.com/{org}/{repo}.git"
ARG BASE_COMMIT

{self.global_env}

LABEL org.opencontainers.image.title="{org}/{repo}" \\
      org.opencontainers.image.description="{org}/{repo} Docker image" \\
      org.opencontainers.image.source="https://github.com/{org}/{repo}" \\
      org.opencontainers.image.authors="https://www.ethara.ai/"

WORKDIR /home/
ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV SHARP_IGNORE_GLOBAL_LIBVIPS=true
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates openssl python3 make g++ && rm -rf /var/lib/apt/lists/*

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
        # Level 2: per-PR image FROM the shared ImageBase toolchain. dependency()
        # is an *Image* (not a string), so the DockerfileEnhancer returns
        # dockerfile() verbatim -- the clone/checkout + Image._HARDENING_BLOCK
        # below are kept exactly as written, and pinning BASE_COMMIT here is
        # correct because this image is per-PR, not the shared base.
        return ImageBase(self.pr, self._config)

    def image_name(self) -> str:
        return (
            f"{self.image_prefix()}/{self.pr.org}_m_{self.pr.repo}".lower()
            if self.image_prefix()
            else f"{self.pr.org}_m_{self.pr.repo}".lower()
        )

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
                "apply_patches.sh",
                r"""#!/bin/bash
# apply_patches.sh <patch>... -- apply patches to the CWD repo, strictly.
#
# The fix patches in this dataset were generated with plain `git diff` rather
# than `git diff --binary`, so every change to a binary file is recorded as a
# contentless placeholder:
#
#     diff --git a/frontend/src/media/x.png b/frontend/src/media/x.png
#     index 1234abc..5678def 100644
#     Binary files a/frontend/src/media/x.png and b/frontend/src/media/x.png differ
#
# There is no blob data in the patch, and the index line is abbreviated, so
# `git apply` rejects the whole patch ("cannot apply binary patch ... without
# full index line") -- for 10 of the 12 PRs here. Those placeholders carry zero
# information, and every one of them is a frontend/extras image asset that the
# server/collector jest suites never load, so we drop just those sections and
# apply everything else strictly. Any other failure is a real failure and must
# abort: applying with `--reject ... || true` would let a partial apply through
# and score tests against a tree that never received the patch.
set -eo pipefail

stripped=()
for p in "$@"; do
  s="${p%.patch}.stripped.patch"
  awk '
    /^diff --git /             { if (n && !skip) printf "%s", buf; buf=""; skip=0; n=1 }
    /^Binary files .* differ$/ { skip=1 }
                               { buf = buf $0 "\n" }
    END                        { if (n && !skip) printf "%s", buf }
  ' "$p" > "$s"

  dropped=$(grep -c '^Binary files .* differ$' "$p" || true)
  if [ "${dropped:-0}" -gt 0 ]; then
    echo "apply_patches: $p: dropped $dropped contentless binary section(s):" >&2
    grep '^Binary files .* differ$' "$p" | sed 's/^/apply_patches:   /' >&2
  fi
  stripped+=("$s")
done

git apply --whitespace=nowarn "${stripped[@]}"
""",
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
# The repo is already cloned and checked out at ${{BASE_COMMIT}} by the
# Dockerfile, so this script performs no git checkout of its own -- doing one
# here would fight the hardening pass that follows it. It only installs
# dependencies and generates the Prisma client, while the network is still
# available, so the eval runs are reproducible. node_modules is untracked, so
# the history strip that runs after this script leaves it in place.
set -e

cd /home/{pr.repo}
git reset --hard
bash /home/check_git_changes.sh

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

yarn install --ignore-scripts || true

if [ -f server/package.json ]; then
  (cd server && yarn install --frozen-lockfile --ignore-scripts) || (cd server && yarn install --ignore-scripts) || true
fi

if [ -f collector/package.json ]; then
  (cd collector && yarn install --frozen-lockfile --ignore-scripts) || (cd collector && yarn install --ignore-scripts) || true
fi

if [ -f server/prisma/schema.prisma ]; then
  (cd server && npx --yes prisma generate) || true
fi

if [ -f server/.env.example ] && [ ! -f server/.env.development ]; then
  cp server/.env.example server/.env.development || true
fi
if [ -f collector/.env.example ] && [ ! -f collector/.env ]; then
  cp collector/.env.example collector/.env || true
fi
if [ -f frontend/.env.example ] && [ ! -f frontend/.env ]; then
  cp frontend/.env.example frontend/.env || true
fi

""".format(pr=self.pr),
            ),
            File(
                ".",
                "run.sh",
                """#!/bin/bash
# Baseline stage: no patch applied. `jest` writes its per-test results to
# stderr, hence the 2>&1 -- without it parse_log sees an empty log.
set -eo pipefail

cd /home/{pr.repo}
export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  # Never succeed silently: a no-op here parses as zero tests, which reads as a
  # clean run rather than a broken instance.
  echo "ERROR: no test runner configured at this commit" >&2
  exit 1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "test-run.sh",
                """#!/bin/bash
# Test-only stage: the test patch MUST apply cleanly. Applying it with
# `--reject ... || true` would let a failed/partial apply fall through and run
# the pre-patch suite, so the harness would score tests that never existed --
# fail_to_pass would be computed from stale results. apply_patches.sh is strict.
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

yarn install --ignore-scripts || true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  echo "ERROR: no test runner configured at this commit" >&2
  exit 1
fi
""".format(pr=self.pr),
            ),
            File(
                ".",
                "fix-run.sh",
                """#!/bin/bash
# Gold stage: test patch + fix patch, both applied in one atomic, strict
# `git apply` (see apply_patches.sh). The previous `--reject ... || true`
# fallback let a partial apply through, which would score the gold patch against
# a tree that never received it.
set -eo pipefail

cd /home/{pr.repo}
bash /home/apply_patches.sh /home/test.patch /home/fix.patch

export PUPPETEER_SKIP_DOWNLOAD=true
export SHARP_IGNORE_GLOBAL_LIBVIPS=true
export CI=true

# The fix patch may add dependencies (every PR in this dataset touches a
# package.json), so refresh node_modules before running the suite.
yarn install --ignore-scripts || true

if [ -f package.json ] && grep -q '"test"' package.json; then
  yarn test --verbose 2>&1
elif [ -d server/__tests__ ] || [ -d collector/__tests__ ]; then
  npx --yes jest --verbose --rootDir=. 2>&1
else
  echo "ERROR: no test runner configured at this commit" >&2
  exit 1
fi
""".format(pr=self.pr),
            ),
        ]

    def dockerfile(self) -> str:
        # Two-level per-PR Dockerfile. The shared toolchain base does NOT clone,
        # so this image clones full history then checks out ${BASE_COMMIT}
        # inline. Because dependency() is an Image, the DockerfileEnhancer
        # returns this Dockerfile verbatim, so the clone + hardening below are
        # kept as written; build_dataset only injects REPO_URL/BASE_COMMIT build
        # args for *string*-dependency images, which is why BASE_COMMIT is
        # defaulted to this PR's sha here. Image._HARDENING_BLOCK is concatenated
        # raw (not through the f-string) so its ${BASE_COMMIT}/%(refname) tokens
        # stay literal. prepare.sh installs node_modules while the network and
        # git history are still available; node_modules is untracked, so the
        # hardening strip that follows leaves it in place for the offline evals.
        image = self.dependency()
        name = image.image_name()
        tag = image.image_tag()

        copy_commands = ""
        for file in self.files():
            copy_commands += f"COPY {file.name} /home/\n"

        header = f"""FROM {name}:{tag}

ARG BASE_COMMIT="{self.pr.base.sha}"
ENV BASE_COMMIT=${{BASE_COMMIT}}

{self.global_env}

WORKDIR /home/
RUN git clone https://github.com/{self.pr.org}/{self.pr.repo}.git /home/{self.pr.repo}

WORKDIR /home/{self.pr.repo}
RUN git reset --hard && git checkout ${{BASE_COMMIT}}

{copy_commands}
RUN bash /home/prepare.sh

"""

        tail = f"""
{self.clear_env}

CMD ["/bin/bash"]
"""
        return header + Image._HARDENING_BLOCK + tail


@Instance.register("Mintplex-Labs", "anything-llm")
class AnythingLLM(Instance):
    def __init__(self, pr: PullRequest, config: Config, *args, **kwargs):
        super().__init__()
        self._pr = pr
        self._config = config

    @property
    def pr(self) -> PullRequest:
        return self._pr

    def dependency(self) -> Optional[Image]:
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
        passed_tests: set[str] = set()
        failed_tests: set[str] = set()
        skipped_tests: set[str] = set()

        ansi_escape = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        re_file = re.compile(
            r"^(?:PASS|FAIL)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)\b"
        )
        re_test = re.compile(
            r"^(\s+)([✓√✕✗×○⊘↓])\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?$"
        )
        re_describe = re.compile(r"^(\s+)([A-Za-z0-9_$#].*?)\s*$")

        ignore_describe_prefixes = (
            "at ",
            "console.",
            "expect(",
            "Expected",
            "Received",
            "Difference:",
        )

        current_file = ""
        describes: list[tuple[int, str]] = []
        name_counts: dict[str, int] = {}

        for raw_line in test_log.splitlines():
            line = ansi_escape.sub("", raw_line)
            if not line.strip():
                continue

            file_match = re_file.match(line)
            if file_match:
                current_file = file_match.group(1)
                describes = []
                continue

            test_match = re_test.match(line)
            if test_match:
                indent = len(test_match.group(1))
                glyph = test_match.group(2)
                leaf = test_match.group(3).strip()
                path_parts = [d[1] for d in describes if d[0] < indent]
                path_parts.append(leaf)
                full = " > ".join(path_parts)
                base_name = f"{current_file}::{full}" if current_file else full
                count = name_counts.get(base_name, 0) + 1
                name_counts[base_name] = count
                name = base_name if count == 1 else f"{base_name}#{count}"
                if glyph in "✓√":
                    passed_tests.add(name)
                elif glyph in "✕✗×":
                    failed_tests.add(name)
                else:
                    skipped_tests.add(name)
                continue

            desc_match = re_describe.match(line)
            if desc_match:
                indent = len(desc_match.group(1))
                text = desc_match.group(2).strip()
                if (
                    indent < 30
                    and not text.startswith(ignore_describe_prefixes)
                    and not text.startswith(("●", "▼", "▶", "›", ">", "|"))
                ):
                    describes = [d for d in describes if d[0] < indent]
                    describes.append((indent, text))

        passed_tests -= failed_tests
        passed_tests -= skipped_tests
        failed_tests -= skipped_tests

        return TestResult(
            passed_count=len(passed_tests),
            failed_count=len(failed_tests),
            skipped_count=len(skipped_tests),
            passed_tests=passed_tests,
            failed_tests=failed_tests,
            skipped_tests=skipped_tests,
        )


# ---------------------------------------------------------------------------
# number_interval auto-population -- REGISTRY-SCOPED shim (no other file edited).
#
# The output dataset jsonl's `number_interval` is written from the loaded
# PullRequest (Dataset.build -> number_interval=pr.number_interval), but the
# bundle's PR list (`prs_in_bundle`) is dropped when the raw record is parsed
# into a PullRequest, and the harness never derives it. The Mintplex-Labs
# dataset carries no `number_interval` field at all, so without this it would
# stay "" in the resolved jsonl.
#
# The interval is the EXACT PRs in the bundle joined with "-", NOT a first-last
# range: prs_in_bundle [146, 147, 150, 155, 157] -> "146-147-150-155-157".
# A "146-157" range would wrongly imply every PR in between is included.
#
# As this must live ONLY in the registry, we install two small, idempotent,
# Mintplex-Labs-scoped shims at import time (this file is the only one changed):
#
#   1. PullRequest.from_json -- for Mintplex-Labs/anything-llm records whose
#      number_interval is empty, fill it from the raw line's prs_in_bundle.
#      That value then flows straight into the output dataset record.
#   2. Instance.create -- a non-empty number_interval makes routing look up
#      `Mintplex-Labs/<that-list>`, which is not a registered key; fall back to
#      `Mintplex-Labs/anything-llm` so the build still routes. Other repos are
#      unaffected: shim 1 only fills Mintplex-Labs, and era-keyed datasets keep
#      their pre-set number_interval (only EMPTY values are filled) whose
#      `org/<era>` key is registered (so the fallback never triggers for them).
#      Shims installed by other registries chain safely: each re-raises when the
#      org/repo is not its own.
# ---------------------------------------------------------------------------
import json as _mp_json  # noqa: E402

_MP_ORG = "Mintplex-Labs"
_MP_REPO = "anything-llm"


def _mp_interval_from_raw(json_str: str) -> str:
    """Return the dash-joined prs_in_bundle for a raw record, or "" if absent.

    Bundle order is preserved as delivered (the dataset ships them ascending);
    values are emitted verbatim so the string round-trips the source list.
    """
    try:
        prs = (_mp_json.loads(json_str) or {}).get("prs_in_bundle") or []
    except Exception:
        return ""
    return "-".join(str(p) for p in prs)


if not getattr(PullRequest, "_mintplex_ni_shim", False):
    _mp_orig_from_json = PullRequest.from_json.__func__

    def _mp_from_json(cls, json_str):
        pr = _mp_orig_from_json(cls, json_str)
        try:
            if (
                getattr(pr, "org", "") == _MP_ORG
                and getattr(pr, "repo", "") == _MP_REPO
                and not getattr(pr, "number_interval", "")
            ):
                interval = _mp_interval_from_raw(json_str)
                if interval:
                    pr.number_interval = interval
        except Exception:
            pass
        return pr

    PullRequest.from_json = classmethod(_mp_from_json)
    PullRequest._mintplex_ni_shim = True

if not getattr(Instance, "_mintplex_route_shim", False):
    _mp_orig_create = Instance.create.__func__

    def _mp_create(cls, pr, config, *args, **kwargs):
        try:
            return _mp_orig_create(cls, pr, config, *args, **kwargs)
        except ValueError:
            if getattr(pr, "org", "") == _MP_ORG and getattr(pr, "repo", "") == _MP_REPO:
                name = f"{pr.org}/{pr.repo}"
                if name in cls._registry:
                    return cls._registry[name](pr, config, *args, **kwargs)
            raise

    Instance.create = classmethod(_mp_create)
    Instance._mintplex_route_shim = True
